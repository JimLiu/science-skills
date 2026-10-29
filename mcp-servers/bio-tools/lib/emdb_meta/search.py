"""Complete paged EMDB search with hit-count verification.

The EMDB search route (/emdb/api/search/{query}) is Solr-backed but does NOT
expose numFound in its payload (the response is a bare result array / CSV
table with no header object). Two other routes over the same service do expose
query-level counts: /facet/{query}?field=current_status and /yearly/{query}.

IMPORTANT API ASYMMETRY (measured 2026-05-30, documented in the README):
the search route returns released AND obsoleted (status OBS) entries, whereas
the facet and yearly routes count released (REL) entries only. run_search_spec
therefore retrieves every page, splits the rows by current_status, and verifies
the RELEASED row count against the facet-route count (the yearly route is used
as a second, independent formulation by the gate). Obsoleted rows are reported
explicitly, never silently mixed into the released count.
"""
from __future__ import annotations

from collections import Counter

from .client import EMDBClient

DEFAULT_FL = "emdb_id,title,resolution,structure_determination_method,fitted_pdbs,current_status,release_date"
PAGE_ROWS = 200


def search_count(client: EMDBClient, query: str) -> int:
    """Number of RELEASED entries matching `query`, via the facet route.

    (The facet/yearly routes index released entries only; obsoleted entries are
    returned by the search route but not counted here.)
    """
    facet = client.facet(query, field="current_status")
    counts = facet.get("current_status") or {}
    return int(sum(counts.values()))


def search_count_yearly(client: EMDBClient, query: str) -> int:
    """Same released-entry count via the independent /yearly route."""
    yearly = client.yearly(query)
    return int(sum(item["value"] for item in yearly.get("annually", [])))


def _emd_sort_key(emdb_id: str) -> int:
    try:
        return int(str(emdb_id).upper().replace("EMD-", ""))
    except ValueError:
        return 1 << 60


def run_search_spec(client: EMDBClient, query: str, *, fl: str = DEFAULT_FL,
                    rows_per_page: int = PAGE_ROWS, max_rows: int = 10000) -> dict:
    """Run one search spec to completion.

    Returns a dict with:
      query                   the query string as submitted
      num_found_released      facet-route count of released entries (ground truth)
      rows_retrieved          total rows retrieved from the search route (REL + OBS)
      rows_by_status          e.g. {"REL": 890, "OBS": 19}
      released_complete       True iff rows with status REL == num_found_released
      records                 full per-entry rows (dicts keyed by the fl fields),
                              sorted by EMD accession number for deterministic output
    """
    if "current_status" not in fl.split(","):
        fl = fl + ",current_status"
    num_found_released = search_count(client, query)
    records: list[dict] = []
    page = 1
    while len(records) < max_rows:
        rows = client.search_page(query, rows=rows_per_page, page=page, fl=fl, as_csv=True)
        if not rows:
            break
        records.extend(rows)
        if len(rows) < rows_per_page:
            break
        page += 1

    seen = set()
    unique = []
    for row in records:
        key = row.get("emdb_id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: _emd_sort_key(r.get("emdb_id", "")))
    status_counts = Counter((row.get("current_status") or "UNKNOWN") for row in unique)
    released_rows = status_counts.get("REL", 0)
    return {
        "query": query,
        "num_found_released": num_found_released,
        "rows_retrieved": len(unique),
        "rows_by_status": dict(sorted(status_counts.items())),
        "released_complete": released_rows == num_found_released,
        "records": unique,
    }
