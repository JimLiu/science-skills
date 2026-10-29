"""Core gene/protein -> Reactome pathway mapping.

Workflow (the part agents routinely get wrong):

1. ``ContentService /data/database/version`` — record the Reactome release the
   mapping was computed against.
2. ``AnalysisService POST /identifiers/`` with the **whole identifier list** —
   the token workflow.  The batch result provides the authoritative
   ``identifiersNotFound`` count, the ``resourceSummary`` totals and the
   per-pathway ``entities.found`` counts that the accuracy gate checks against.
3. One single-identifier ``AnalysisService POST /identifiers/`` per input —
   this yields the per-identifier pathway set (stable ID, name, species,
   low-level flag) without any per-pathway token traversal.

Everything is returned as one deterministic JSON-able dict: genes in input
order, pathways sorted by stable ID, volatile values (analysis tokens,
timestamps, request log) confined to the ``provenance`` block so that
run-to-run comparisons can simply drop that one key.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from .client import ReactomeClient

VALID_ID_TYPES = ("symbol", "uniprot")



VOLATILE_TOP_LEVEL_KEYS = ("provenance",)


def _pathway_record(p: dict[str, Any]) -> dict[str, Any]:
    """Normalize one AnalysisService pathway entry into the tool's stable schema."""
    species = p.get("species") or {}
    entities = p.get("entities") or {}
    reactions = p.get("reactions") or {}
    return {
        "stId": p.get("stId"),
        "dbId": p.get("dbId"),
        "name": p.get("name"),
        "species": species.get("name"),
        "taxId": species.get("taxId"),
        "llp": bool(p.get("llp", False)),
        "inDisease": bool(p.get("inDisease", False)),
        "entities_found": entities.get("found"),
        "entities_total": entities.get("total"),
        "entities_ratio": entities.get("ratio"),
        "entities_pvalue": entities.get("pValue"),
        "entities_fdr": entities.get("fdr"),
        "reactions_found": reactions.get("found"),
        "reactions_total": reactions.get("total"),
        "resource": entities.get("resource"),
    }


def _sorted_pathways(pathways: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(pathways, key=lambda p: (p["stId"] or "", p["name"] or ""))


def map_identifiers(
    identifiers: list[str],
    id_type: str,
    *,
    species: str = "Homo sapiens",
    include_disease: bool = True,
    interactors: bool = False,
    projection: bool = False,
    resource: str = "TOTAL",
    client: ReactomeClient | None = None,
) -> dict[str, Any]:
    """Map gene symbols or UniProt accessions to Reactome low-level pathways.

    Parameters
    ----------
    identifiers:
        Gene symbols (``id_type='symbol'``) or UniProt accessions
        (``id_type='uniprot'``).  Order is preserved in the output.
    id_type:
        ``'symbol'`` or ``'uniprot'`` — recorded in the output; the
        AnalysisService maps both transparently.
    species:
        Pathways are filtered to this species name (default ``Homo sapiens``).
    include_disease, interactors, projection:
        Passed through to the AnalysisService (defaults mirror the service
        defaults: disease pathways included, no interactors, no projection).
    resource:
        AnalysisService molecule-resource view (``TOTAL`` default, or
        ``UNIPROT``, ``ENSEMBL``, ...).  ``UNIPROT`` restricts the result to
        protein-level mappings — the like-for-like view when comparing a gene
        symbol submission with a UniProt accession submission, because a
        symbol additionally matches gene/transcript-level (Ensembl) entities
        that an accession cannot.
    client:
        Optional pre-configured :class:`ReactomeClient` (used by tests to
        inject a mock transport).

    Returns
    -------
    dict with keys ``tool, reactome_version, id_type, species, parameters,
    n_input, identifiers, genes, batch_summary, provenance``.
    """
    if id_type not in VALID_ID_TYPES:
        raise ValueError(f"id_type must be one of {VALID_ID_TYPES}, got {id_type!r}")
    if not identifiers:
        raise ValueError("identifiers list is empty")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("identifiers list contains duplicates")

    own_client = client is None
    cl = client or ReactomeClient()
    started = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        version = cl.database_version()


        batch = cl.analyse_identifiers(
            identifiers,
            sample_name=f"reactome-map batch ({id_type})",
            interactors=interactors,
            include_disease=include_disease,
            projection=projection,
            resource=resource,
        )
        batch_token = (batch.get("summary") or {}).get("token")
        batch_not_found_count = batch.get("identifiersNotFound", 0)
        batch_not_found: list[str] = []
        if batch_not_found_count:
            nf = cl.analysis_not_found(batch_token)
            batch_not_found = sorted(
                e.get("id") if isinstance(e, dict) else str(e) for e in nf
            )

        batch_pathways_all = [_pathway_record(p) for p in batch.get("pathways", [])]
        batch_pathways = _sorted_pathways(
            [p for p in batch_pathways_all if species is None or p["species"] == species]
        )
        batch_summary = {
            "n_submitted": len(identifiers),
            "identifiers_not_found_count": batch_not_found_count,
            "identifiers_not_found": batch_not_found,
            "identifiers_found_count": len(identifiers) - batch_not_found_count,
            "pathways_found": batch.get("pathwaysFound"),
            "pathways_returned_total": len(batch_pathways_all),
            "resource_summary": sorted(
                (
                    {"resource": r.get("resource"), "pathways": r.get("pathways"),
                     "filtered": r.get("filtered")}
                    for r in batch.get("resourceSummary", [])
                ),
                key=lambda r: r["resource"] or "",
            ),
            "pathways": batch_pathways,
        }


        genes: dict[str, Any] = {}
        per_gene_tokens: dict[str, str | None] = {}
        for ident in identifiers:
            single = cl.analyse_identifiers(
                [ident],
                sample_name=f"reactome-map {ident}",
                interactors=interactors,
                include_disease=include_disease,
                projection=projection,
                resource=resource,
            )
            per_gene_tokens[ident] = (single.get("summary") or {}).get("token")
            not_found = single.get("identifiersNotFound", 0)
            pathways_all = [_pathway_record(p) for p in single.get("pathways", [])]
            pathways = _sorted_pathways(
                [p for p in pathways_all if species is None or p["species"] == species]
            )
            genes[ident] = {
                "query": ident,
                "id_type": id_type,
                "found": not_found == 0,
                "pathways_found_api": single.get("pathwaysFound"),
                "n_pathways_all_species": len(pathways_all),
                "n_pathways": len(pathways),
                "n_lowlevel_pathways": sum(1 for p in pathways if p["llp"]),
                "lowlevel_pathway_stids": [p["stId"] for p in pathways if p["llp"]],
                "pathways": pathways,
            }

        result: dict[str, Any] = {
            "tool": "reactome-map",
            "reactome_version": version,
            "id_type": id_type,
            "species": species,
            "parameters": {
                "include_disease": include_disease,
                "interactors": interactors,
                "projection": projection,
                "resource": resource,
            },
            "n_input": len(identifiers),
            "identifiers": list(identifiers),
            "genes": genes,
            "batch_summary": batch_summary,
            "provenance": {
                "started_utc": started,
                "finished_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "analysis_service": "https://reactome.org/AnalysisService",
                "content_service": "https://reactome.org/ContentService",
                "reactome_version": version,
                "batch_token": batch_token,
                "per_gene_tokens": per_gene_tokens,
                "http_requests": cl.http_requests,
                "bytes_downloaded": cl.bytes_downloaded,
                "request_log": [vars(r) for r in cl.log],
            },
        }
        return result
    finally:
        if own_client:
            cl.close()


def stable_view(result: dict[str, Any]) -> dict[str, Any]:
    """Return the run-to-run-stable portion of a mapping result.

    Drops the ``provenance`` block (tokens, timestamps, request log) — every
    remaining byte must be identical across repeated runs against the same
    Reactome release.
    """
    return {k: v for k, v in result.items() if k not in VOLATILE_TOP_LEVEL_KEYS}


def canonical_json(obj: Any) -> str:
    """Canonical serialization used for run-to-run identity checks."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compact_view(result: dict[str, Any]) -> dict[str, Any]:
    """The tool's primary (agent-facing) output: per gene, the low-level pathways.

    Each input identifier maps to a list of ``{stId, name, species}`` records for
    the low-level pathways (``llp == True``) it participates in, plus a small
    header (Reactome version, id type, species filter, per-gene counts).  The
    full statistics (p-values, entity counts, batch summary, provenance) remain
    available in the full result; this view is what an agent ingests when it
    just needs the gene -> pathway mapping.
    """
    genes_out = {}
    for ident in result["identifiers"]:
        g = result["genes"][ident]
        genes_out[ident] = {
            "found": g["found"],
            "n_lowlevel_pathways": g["n_lowlevel_pathways"],
            "pathways": [
                {"stId": p["stId"], "name": p["name"], "species": p["species"]}
                for p in g["pathways"]
                if p["llp"]
            ],
        }
    return {
        "tool": result["tool"],
        "reactome_version": result["reactome_version"],
        "id_type": result["id_type"],
        "species": result["species"],
        "n_input": result["n_input"],
        "genes": genes_out,
    }
