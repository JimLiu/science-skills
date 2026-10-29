"""High-level operations: study fetch, per-study analysis listings, biome/search listings."""

from __future__ import annotations

from typing import Any

from mcp_servers_common.pathseg import seg

from .client import MGnifyClient, MGnifyNotFound
from .records import (
    ANALYSIS_COLUMNS,
    LISTING_COLUMNS,
    STUDY_COLUMNS,
    analysis_count_breakdowns,
    flatten_analysis,
    flatten_study,
    to_tsv,
)


def fetch_study_analyses(accession: str, client: MGnifyClient | None = None) -> dict[str, Any]:
    """All analyses of one study (complete pagination), sorted by MGYA accession.

    Returns ``{"study_accession", "analyses_count", "analyses": [record, ...]}``.
    ``analyses_count`` is the API's ``meta.pagination.count`` (the retrieval is
    verified against it inside :meth:`MGnifyClient.get_all`).
    """
    client = client or MGnifyClient()
    res = client.get_all(
        f"studies/{seg(accession)}/analyses")
    records = sorted(
        (flatten_analysis(o, accession) for o in res["records"]),
        key=lambda r: r["analysis_accession"],
    )
    return {"study_accession": accession, "analyses_count": res["count"], "analyses": records}


def fetch_studies(
    accessions: list[str],
    client: MGnifyClient | None = None,
    include_analyses: bool = True,
) -> dict[str, Any]:
    """Structured records for a list of MGYS study accessions.

    For each accession: one GET on ``/studies/{accession}`` plus (when
    ``include_analyses``) a fully paginated retrieval of
    ``/studies/{accession}/analyses``. Unknown accessions are reported in
    ``"missing"`` rather than silently dropped. Output order is deterministic
    (accessions sorted, analyses sorted by MGYA accession).
    """
    client = client or MGnifyClient()
    studies: list[dict[str, Any]] = []
    analyses: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []


    for acc in sorted(set(accessions)):
        try:
            doc = client.get_json(f"studies/{seg(acc)}")
        except MGnifyNotFound:
            missing.append(acc)
            continue
        rec = flatten_study(doc["data"])
        if include_analyses:
            ana = fetch_study_analyses(acc, client=client)
            rec["analyses_total"] = ana["analyses_count"]
            breakdown = analysis_count_breakdowns(ana["analyses"])
            rec["analyses_by_pipeline_version"] = breakdown["by_pipeline_version"]
            rec["analyses_by_experiment_type"] = breakdown["by_experiment_type"]
            analyses[acc] = ana["analyses"]
        studies.append(rec)
    out: dict[str, Any] = {"studies": studies, "missing": sorted(missing)}
    if include_analyses:
        out["analyses"] = analyses
    return out


def search_studies(spec: dict[str, Any], client: MGnifyClient | None = None) -> dict[str, Any]:
    """Complete paged study listing for a declarative spec.

    ``spec`` is either ``{"type": "biome", "lineage": "root:..."}`` (studies under a
    biome lineage, including sub-lineages, via ``/biomes/{lineage}/studies``) or
    ``{"type": "search", "query": "..."}`` (free-text search via ``/studies?search=``).
    The retrieval is verified against ``meta.pagination.count``; records are sorted
    by MGYS accession.
    """
    client = client or MGnifyClient()
    if spec["type"] == "biome":
        path = f"biomes/{seg(spec['lineage'], safe=':')}/studies"
        params: dict[str, Any] = {}
    elif spec["type"] == "search":
        path = "studies"
        params = {"search": spec["query"]}
    else:
        raise ValueError(f"unknown spec type: {spec['type']!r}")
    res = client.get_all(path, params=params)
    records = sorted((flatten_study(o) for o in res["records"]), key=lambda r: r["accession"])
    return {
        "spec": spec,
        "count": res["count"],
        "pages_fetched": res["pages_fetched"],
        "records": records,
    }


def studies_tsv(study_records: list[dict[str, Any]]) -> str:
    """TSV table of study records (fixed column order)."""
    return to_tsv(study_records, STUDY_COLUMNS)


def analyses_tsv(analysis_records: list[dict[str, Any]]) -> str:
    """TSV table of analysis records (fixed column order)."""
    return to_tsv(analysis_records, ANALYSIS_COLUMNS)


def listing_tsv(listing_records: list[dict[str, Any]]) -> str:
    """TSV table of biome/search listing records (fixed column order)."""
    return to_tsv(listing_records, LISTING_COLUMNS)
