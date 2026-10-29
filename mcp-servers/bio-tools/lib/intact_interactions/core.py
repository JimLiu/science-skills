"""Retrieve binary interactions from the IntAct web service.

Main entry point: :func:`fetch_interactions` — protein/gene query ->
complete, MI-score-filtered, deterministically ordered list of slim
binary-interaction records, with the server-reported ``totalElements``
carried alongside so callers can verify completeness.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .client import IntActClient, IntActError




DEFAULT_PAGE_SIZE = 500

SEARCH_PATH = "interaction/findInteractionWithFacet"
NAIVE_PATH = "interaction/findInteractions"
INTERACTOR_PATH = "interactor/findInteractor"
COUNT_PATH = "interaction/countInteractionResult"






def _strip_db_suffix(identifier: str | None) -> str | None:
    """'P04637 (uniprotkb)' -> 'P04637'; passthrough for None."""
    if identifier is None:
        return None
    return identifier.split(" (", 1)[0].strip()


def slim_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw IntAct SearchInteraction document (~100 fields, ~24 kB)
    to the structured fields this tool exposes.

    Scientific content is copied verbatim from the raw document; the only
    transformation is dropping unrequested fields and stripping the
    ' (databasename)' suffix from participant identifiers (the database of
    origin is preserved separately via id_a_database / id_b_database).
    """
    id_a_raw = raw.get("idA")
    id_b_raw = raw.get("idB")
    return {
        "interaction_ac": raw.get("ac"),
        "binary_interaction_id": raw.get("binaryInteractionId"),
        "ac_a": raw.get("acA"),
        "ac_b": raw.get("acB"),
        "id_a": _strip_db_suffix(id_a_raw),
        "id_b": _strip_db_suffix(id_b_raw),
        "id_a_database": (
            id_a_raw.split(" (", 1)[1].rstrip(")") if id_a_raw and " (" in id_a_raw else None
        ),
        "id_b_database": (
            id_b_raw.split(" (", 1)[1].rstrip(")") if id_b_raw and " (" in id_b_raw else None
        ),
        "molecule_a": raw.get("moleculeA"),
        "molecule_b": raw.get("moleculeB"),
        "species_a": raw.get("speciesA"),
        "species_b": raw.get("speciesB"),
        "taxid_a": raw.get("taxIdA"),
        "taxid_b": raw.get("taxIdB"),
        "interaction_type": raw.get("type"),
        "interaction_type_mi": raw.get("typeMIIdentifier"),
        "detection_method": raw.get("detectionMethod"),
        "detection_method_mi": raw.get("detectionMethodMIIdentifier"),
        "experimental_role_a": raw.get("experimentalRoleA"),
        "experimental_role_b": raw.get("experimentalRoleB"),
        "host_organism": raw.get("hostOrganism"),
        "expansion_method": raw.get("expansionMethod"),
        "mi_score": raw.get("intactMiscore"),
        "negative": raw.get("negative"),
        "pubmed_id": raw.get("publicationPubmedIdentifier"),
        "first_author": raw.get("firstAuthor"),
        "source_database": raw.get("sourceDatabase"),
    }


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic order: descending MI score, then interaction AC, then
    binary interaction id (the latter two break ties uniquely)."""
    return sorted(
        records,
        key=lambda r: (
            -(r["mi_score"] if r["mi_score"] is not None else -1.0),
            r["interaction_ac"] or "",
            r["binary_interaction_id"] or 0,
        ),
    )


def canonical_json(obj: Any) -> str:
    """Canonical serialization used for run-to-run comparison and tokens."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))






def fetch_interactions(
    query: str,
    min_mi_score: float = 0.0,
    max_mi_score: float = 1.0,
    page_size: int = DEFAULT_PAGE_SIZE,
    interactor_species: list[str] | None = None,
    client: IntActClient | None = None,
) -> dict[str, Any]:
    """Retrieve ALL binary interactions matching ``query`` with
    ``min_mi_score <= intact-miscore <= max_mi_score``.

    Pagination is explicit: pages of ``page_size`` are requested until the
    number of collected records equals the server-reported ``totalElements``.
    The function raises :class:`IntActError` if the count drifts mid-sweep,
    if duplicate records are returned, or if the final tally disagrees with
    ``totalElements`` — silent truncation is never possible.

    Returns a dict::

        {
          "query": str,
          "min_mi_score": float,
          "total_elements": int,        # server-reported
          "n_records": int,             # == total_elements (verified)
          "records": [slim_record, ...] # deterministic order
        }
    """
    own_client = client is None
    if client is None:
        client = IntActClient()
    try:
        params: dict[str, Any] = {
            "query": query,
            "minMIScore": min_mi_score,
            "maxMIScore": max_mi_score,
            "pageSize": page_size,
        }
        if interactor_species:
            params["interactorSpeciesFilter"] = interactor_species

        records: list[dict[str, Any]] = []
        seen_ids: set[Any] = set()
        total_elements: int | None = None
        page = 0
        while True:
            payload = client.post_json(SEARCH_PATH, {**params, "page": page})
            data = payload.get("data", {})
            page_total = data.get("totalElements")
            if page_total is None:
                raise IntActError(
                    f"query {query!r}: response page {page} lacked totalElements"
                )
            if total_elements is None:
                total_elements = page_total
            elif page_total != total_elements:
                raise IntActError(
                    f"query {query!r}: totalElements changed mid-sweep "
                    f"({total_elements} -> {page_total} on page {page})"
                )
            content = data.get("content", [])
            for raw in content:
                key = (raw.get("ac"), raw.get("binaryInteractionId"))
                if key in seen_ids:
                    raise IntActError(
                        f"query {query!r}: duplicate record {key} on page {page}"
                    )
                seen_ids.add(key)
                records.append(slim_record(raw))
            if data.get("last", True) or not content:
                break
            page += 1

        if len(records) != total_elements:
            raise IntActError(
                f"query {query!r}: collected {len(records)} records but server "
                f"reported totalElements={total_elements}"
            )

        return {
            "query": query,
            "min_mi_score": min_mi_score,
            "total_elements": total_elements,
            "n_records": len(records),
            "records": sort_records(records),
        }
    finally:
        if own_client:
            client.close()






def resolve_interactors(
    query: str, client: IntActClient | None = None, page_size: int = 100
) -> list[dict[str, Any]]:
    """Resolve a query (e.g. a UniProt accession) to IntAct interactor
    records via the interactor-centric endpoint ``/interactor/findInteractor``.

    Returns slim interactor records (interactor_ac, preferred_identifier,
    name, species, taxid, type, interaction_count).
    """
    own_client = client is None
    if client is None:
        client = IntActClient()
    try:
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            payload = client.get_json(
                f"{INTERACTOR_PATH}/{urllib.parse.quote(str(query), safe='')}",
                {"page": page, "pageSize": page_size},
            )
            for raw in payload.get("content", []):
                out.append(
                    {
                        "interactor_ac": raw.get("interactorAc"),
                        "preferred_identifier": _strip_db_suffix(
                            raw.get("interactorPreferredIdentifier")
                        ),
                        "name": raw.get("interactorName"),
                        "species": raw.get("interactorSpecies"),
                        "taxid": raw.get("interactorTaxId"),
                        "interactor_type": raw.get("interactorType"),
                        "interaction_count": raw.get("interactionCount"),
                    }
                )
            if payload.get("last", True) or not payload.get("content"):
                break
            page += 1
        return out
    finally:
        if own_client:
            client.close()


def count_interactions_for_interactor(
    query: str,
    interactor_ac: str,
    min_mi_score: float = 0.0,
    max_mi_score: float = 1.0,
    client: IntActClient | None = None,
) -> int:
    """Server-side count of interactions for one interactor AC under the same
    query and MI-score filter (``GET /interaction/countInteractionResult``).

    This is the interactor-centric formulation used to cross-check the
    paginated sweep in the accuracy gate.
    """
    own_client = client is None
    if client is None:
        client = IntActClient()
    try:
        result = client.get_json(
            COUNT_PATH,
            {
                "query": query,
                "interactorAc": interactor_ac,
                "minMIScore": min_mi_score,
                "maxMIScore": max_mi_score,
            },
        )
        if not isinstance(result, int):
            raise IntActError(
                f"countInteractionResult returned non-integer payload: {result!r}"
            )
        return result
    finally:
        if own_client:
            client.close()






def naive_first_page(query: str, client: IntActClient | None = None) -> dict[str, Any]:
    """The naive pattern this tool replaces: a single GET to
    ``/interaction/findInteractions/{query}`` with no paging parameters and
    no MI-score filter. The service returns the first 20 records only and
    nothing in the response forces the caller to notice the truncation.

    Returned dict mirrors fetch_interactions' shape (records are slimmed the
    same way) plus ``raw_response`` for byte/token accounting.
    """
    own_client = client is None
    if client is None:
        client = IntActClient()
    try:
        payload = client.get_json(f"{NAIVE_PATH}/{urllib.parse.quote(str(query), safe='')}")
        content = payload.get("content", [])
        return {
            "query": query,
            "total_elements": payload.get("totalElements"),
            "n_records": len(content),
            "records": [slim_record(raw) for raw in content],
            "raw_response": payload,
        }
    finally:
        if own_client:
            client.close()
