"""Top-level fetch(): declarative spec -> complete, deterministic, trimmed result."""

from __future__ import annotations

from typing import Any

from .client import CTGovClient, DEFAULT_FIELDS
from .query import build_query_params
from .records import trim_study
from .spec import FilterSpec


def fetch(spec: FilterSpec | dict[str, Any],
          client: CTGovClient | None = None,
          fields: str = DEFAULT_FIELDS) -> dict[str, Any]:
    """Retrieve ALL studies matching `spec` from ClinicalTrials.gov v2.

    Returns a dict with:
      spec_id, filters       : echo of the request
      api_total_count        : totalCount reported by the API on the first page
      n_studies              : number of studies returned after full pagination (and dedup)
      nct_ids                : sorted list of NCT IDs
      studies                : trimmed records, sorted by NCT ID
      local_post_filters_applied : always [] (all dimensions are server-side)
      provenance             : one entry per HTTP request made (url, params, page, bytes, ...)
    """
    if isinstance(spec, dict):
        spec = FilterSpec.from_dict(spec)
    spec.validate()

    own_client = client is None
    if own_client:
        client = CTGovClient()
    try:
        params = build_query_params(spec)
        raw_studies, total_count, provenance = client.paginate_studies(params, fields=fields)
    finally:
        if own_client:
            client.close()

    records = [trim_study(s) for s in raw_studies]

    by_id: dict[str, dict] = {}
    for rec in records:
        nct = rec.get("nctId") or ""
        if nct not in by_id:
            by_id[nct] = rec
    nct_ids = sorted(by_id)
    studies = [by_id[n] for n in nct_ids]

    return {
        "spec_id": spec.spec_id,
        "filters": spec.to_dict()["filters"],
        "api_total_count": total_count,
        "n_studies": len(studies),
        "nct_ids": nct_ids,
        "studies": studies,
        "local_post_filters_applied": [],
        "provenance": provenance,
    }
