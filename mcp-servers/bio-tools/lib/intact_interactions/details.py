"""Per-interactor detail, per-interaction-AC detail, and depth-1 network expansion.

Closes the three PARTIAL coverage items vs tooluniverse/intact:
  intact_get_interactor          -> get_interactor(query)
  intact_get_interaction_details -> get_interaction_details(interaction_ac)
  intact_get_interaction_network -> build_network(seeds, ...)

Routes (same www.ebi.ac.uk host as the search routes; <= 2 req/s politeness):
  GET /intact/ws/interactor/findInteractor/{query}      (search service)
  GET /intact/ws/graph/interaction/details/{ac}         (graph service)
  GET /intact/ws/graph/participants/details/{ac}        (graph service)

The graph-service detail routes answer UNKNOWN accessions with HTTP 200 and an
empty body — get_interaction_details maps that to an explicit
``{"interaction_ac": ..., "error": "not_found"}`` record.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from .client import IntActClient
from .core import fetch_interactions, resolve_interactors, sort_records

GRAPH_DETAILS_PATH = "graph/interaction/details"
GRAPH_PARTICIPANTS_PATH = "graph/participants/details"






def _cv(node: Any) -> dict | None:
    """{'shortName': s, 'identifier': mi} -> {'name': s, 'mi': mi}."""
    if not isinstance(node, dict):
        return None
    return {"name": node.get("shortName"), "mi": node.get("identifier")}


def _xref(node: dict) -> dict:
    return {
        "database": (node.get("database") or {}).get("shortName"),
        "database_mi": (node.get("database") or {}).get("identifier"),
        "identifier": node.get("identifier"),
        "qualifier": (node.get("qualifier") or {}).get("shortName")
        if isinstance(node.get("qualifier"), dict) else node.get("qualifier"),
    }






def get_interactor(query: str, client: IntActClient | None = None) -> dict[str, Any]:
    """Standalone interactor detail for one query (UniProt accession, gene
    symbol, or IntAct interactor AC like ``EBI-7090529``).

    Wraps the interactor-centric search route and returns ALL matching
    interactor records (a UniProt accession can resolve to the canonical
    protein plus chain/isoform interactors) with an explicit
    ``n_matches`` — never silently picking one.
    """
    own = client is None
    if client is None:
        client = IntActClient()
    try:
        matches = sorted(
            resolve_interactors(query, client=client),
            key=lambda m: (m["interactor_ac"] or ""),
        )
        return {
            "query": query,
            "n_matches": len(matches),
            "interactors": matches,
        }
    finally:
        if own:
            client.close()






def get_interaction_details(
    interaction_ac: str,
    include_participants: bool = True,
    client: IntActClient | None = None,
) -> dict[str, Any]:
    """Full curated detail for ONE interaction AC (e.g. ``EBI-15635490``):
    type, host organism, detection method, publication, xrefs, annotations,
    parameters, confidences — plus per-participant records (identifier,
    species, biological/experimental role, detection methods) unless
    ``include_participants=False``.

    Unknown ACs return ``{"interaction_ac": ..., "error": "not_found"}``
    (the upstream route answers them with an empty 200 body).
    """
    own = client is None
    if client is None:
        client = IntActClient()
    try:
        raw = client.get_json(
            f"{GRAPH_DETAILS_PATH}/{urllib.parse.quote(str(interaction_ac), safe='')}",
            allow_empty=True)
        if not raw:
            return {"interaction_ac": interaction_ac, "error": "not_found"}

        pub = raw.get("publication") or {}
        record: dict[str, Any] = {
            "interaction_ac": raw.get("interactionAc"),
            "short_label": raw.get("shortLabel"),
            "type": _cv(raw.get("type")),
            "detection_method": _cv(raw.get("detectionMethod")),
            "host_organism": raw.get("hostOrganism"),
            "negative": raw.get("negative"),
            "publication": {
                "pubmed_id": pub.get("pubmedId"),
                "title": pub.get("title"),
                "journal": pub.get("journal"),
                "year": pub.get("year"),
                "authors": pub.get("authors"),
            } if pub else None,
            "xrefs": sorted(
                (_xref(x) for x in raw.get("xrefs") or [] if isinstance(x, dict)),
                key=lambda x: (x["database"] or "", x["identifier"] or ""),
            ),
            "annotations": [
                {"topic": (_cv(a.get("topic")) or {}).get("name"),
                 "topic_mi": (_cv(a.get("topic")) or {}).get("mi"),
                 "description": a.get("description")}
                for a in raw.get("annotations") or [] if isinstance(a, dict)
            ],
            "parameters": raw.get("parameters") or [],
            "confidences": raw.get("confidences") or [],
        }

        if include_participants:
            participants = []
            page = 0
            while True:
                payload = client.get_json(
                    f"{GRAPH_PARTICIPANTS_PATH}/"
                    f"{urllib.parse.quote(str(interaction_ac), safe='')}",
                    {"page": page, "pageSize": 100},
                )
                for p in payload.get("content", []):
                    pid = p.get("participantId") or {}
                    participants.append({
                        "participant_ac": p.get("participantAc"),
                        "short_label": p.get("shortLabel"),
                        "identifier": pid.get("identifier"),
                        "identifier_database": (pid.get("database") or {}).get("shortName"),
                        "description": p.get("description"),
                        "type": _cv(p.get("type")),
                        "species": (p.get("species") or {}).get("scientificName"),
                        "taxid": (p.get("species") or {}).get("taxId"),
                        "biological_role": _cv(p.get("biologicalRole")),
                        "experimental_role": _cv(p.get("experimentalRole")),
                        "detection_methods": [_cv(m) for m in p.get("detectionMethod") or []],
                    })
                if payload.get("last", True) or not payload.get("content"):
                    break
                page += 1
            participants.sort(key=lambda p: (p["participant_ac"] or ""))
            record["participants"] = participants
            record["n_participants"] = len(participants)

        return record
    finally:
        if own:
            client.close()






def build_network(
    seeds: list[str],
    min_mi_score: float = 0.45,
    max_interactors_expanded: int = 25,
    interactor_species: list[str] | None = None,
    client: IntActClient | None = None,
) -> dict[str, Any]:
    """Depth-1 interaction network around ``seeds`` (UniProt accessions).

    Step 1: complete MI-score-filtered sweep per seed (the tool's existing
    ``fetch_interactions`` — totalElements-verified, never truncated).
    Step 2: collect the partner identifiers of every seed edge; the partners
    plus the seeds are the network's node set.
    Step 3 (expansion bookkeeping): edges between two NON-seed partners are
    only retrievable by querying the partners themselves; this function
    queries up to ``max_interactors_expanded`` partners (deterministically:
    most-connected first, ties by identifier) and keeps only edges whose BOTH
    endpoints are already in the node set.

    The cap is explicit in the output (``expansion`` block: which partners
    were expanded, which were not) — an uncapped expansion of a hub protein
    would issue thousands of requests; truncation is never silent.
    """
    own = client is None
    if client is None:
        client = IntActClient()
    try:
        seeds = list(dict.fromkeys(seeds))
        edges: dict[tuple, dict] = {}
        node_ids: set[str] = set(seeds)
        partner_degree: dict[str, int] = {}

        seed_sweeps = {}
        for seed in seeds:
            sweep = fetch_interactions(
                seed, min_mi_score=min_mi_score,
                interactor_species=interactor_species, client=client,
            )
            seed_sweeps[seed] = {
                "total_elements": sweep["total_elements"],
                "n_records": sweep["n_records"],
            }
            for rec in sweep["records"]:
                key = (rec["interaction_ac"], rec["binary_interaction_id"])
                edges.setdefault(key, {**rec, "origin": "seed_sweep"})
                for pid in (rec["id_a"], rec["id_b"]):
                    if pid and pid not in seeds:
                        node_ids.add(pid)
                        partner_degree[pid] = partner_degree.get(pid, 0) + 1


        expansion_order = sorted(partner_degree, key=lambda p: (-partner_degree[p], p))
        expanded = expansion_order[:max_interactors_expanded]
        not_expanded = expansion_order[max_interactors_expanded:]

        for partner in expanded:
            sweep = fetch_interactions(
                partner, min_mi_score=min_mi_score,
                interactor_species=interactor_species, client=client,
            )
            for rec in sweep["records"]:
                if rec["id_a"] in node_ids and rec["id_b"] in node_ids:
                    key = (rec["interaction_ac"], rec["binary_interaction_id"])
                    edges.setdefault(key, {**rec, "origin": "partner_expansion"})

        edge_list = sort_records(list(edges.values()))
        return {
            "seeds": seeds,
            "min_mi_score": min_mi_score,
            "n_nodes": len(node_ids),
            "nodes": sorted(node_ids),
            "n_edges": len(edge_list),
            "edges": edge_list,
            "seed_sweeps": seed_sweeps,
            "expansion": {
                "max_interactors_expanded": max_interactors_expanded,
                "n_partners": len(expansion_order),
                "expanded": expanded,
                "not_expanded": not_expanded,
                "complete": not not_expanded,
            },
        }
    finally:
        if own:
            client.close()
