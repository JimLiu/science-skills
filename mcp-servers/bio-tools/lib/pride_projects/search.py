"""Declarative project search and per-accession detail fetch for PRIDE Archive v2.

A search spec is a plain dict::

    {
      "keyword":    "phosphoproteome",          # optional free-text keyword
      "organism":   "Homo sapiens (human)",      # optional, exact facet value
      "instrument": "Orbitrap Eclipse",          # optional, exact facet value
      "disease":    "Covid-19",                  # optional, exact facet value
      "extra_filters": {"experimentTypes": "..."} # optional extra field==value filters
    }

``search_projects`` turns the spec into the upstream ``keyword`` + ``filter``
parameters, then walks ALL result pages (pageSize=100, sorted by accession
ascending so pagination is stable) and returns every hit as a normalized
record, plus the API's own reported total (the ``total_records`` response
header) for verification.
"""

from __future__ import annotations

from mcp_servers_common.pathseg import seg

from .client import PrideClient
from .records import normalize_detail_record, normalize_search_record

PAGE_SIZE = 100
MAX_PAGES = 200


_FILTER_FIELDS = {
    "organism": "organisms",
    "instrument": "instruments",
    "disease": "diseases",
}


class TotalMismatchError(RuntimeError):
    """Raised when the number of retrieved records disagrees with the API total."""


def build_filter(spec: dict) -> str | None:
    """Build the comma-separated ``field==value`` filter string from a spec."""
    parts: list[str] = []
    for key, field in _FILTER_FIELDS.items():
        value = spec.get(key)
        if value:
            parts.append(f"{field}=={value}")
    for field, value in (spec.get("extra_filters") or {}).items():
        if value:
            parts.append(f"{field}=={value}")
    return ",".join(parts) if parts else None


def search_projects(
    client: PrideClient,
    spec: dict,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> dict:
    """Run a declarative search spec to completion.

    Returns ``{"spec", "filter", "api_total", "records", "pages_fetched"}`` where
    ``records`` contains ALL hits (one normalized record per project), sorted by
    accession, and ``api_total`` is the total reported by the API in the
    ``total_records`` response header of the first page.

    Raises ``TotalMismatchError`` if the number of unique retrieved accessions
    does not equal the API-reported total (e.g. the index changed mid-pagination).
    """
    params_base: dict = {
        "pageSize": page_size,
        "sortFields": "accession",
        "sortDirection": "ASC",
    }
    keyword = (spec.get("keyword") or "").strip()
    if keyword:
        params_base["keyword"] = keyword
    filt = build_filter(spec)
    if filt:
        params_base["filter"] = filt

    records: dict[str, dict] = {}
    api_total: int | None = None
    page = 0
    pages_fetched = 0
    complete = False
    while page < max_pages:
        resp = client.get("/search/projects", params={**params_base, "page": page})
        pages_fetched += 1
        header_total = resp.headers.get("total_records")
        if api_total is None and header_total is not None:
            api_total = int(header_total)
        items = resp.json()
        if not items:
            complete = True
            break
        for raw in items:
            rec = normalize_search_record(raw)
            records[rec["accession"]] = rec
        if len(items) < page_size:
            complete = True
            break
        page += 1






    if not complete and api_total is not None and len(records) == api_total:
        complete = True

    ordered = [records[acc] for acc in sorted(records)]





    if complete and api_total is not None and len(ordered) != api_total:
        raise TotalMismatchError(
            f"retrieved {len(ordered)} unique projects but API reported total_records={api_total} "
            f"(spec={spec!r})"
        )
    return {
        "spec": spec,
        "filter": filt,
        "api_total": api_total,
        "records": ordered,
        "complete": complete,
        "pages_fetched": pages_fetched,
    }


def search_first_page_naive(client: PrideClient, spec: dict) -> dict:
    """The NAIVE baseline: a single request with the API's default page size.

    This is what an unsophisticated script (or a copy-pasted curl command) does:
    it silently truncates any result set larger than the default page size and
    returns the raw, un-normalized payload.
    """
    params: dict = {}
    keyword = (spec.get("keyword") or "").strip()
    if keyword:
        params["keyword"] = keyword
    filt = build_filter(spec)
    if filt:
        params["filter"] = filt
    resp = client.get("/search/projects", params=params or None)
    items = resp.json()
    return {
        "spec": spec,
        "api_total": int(resp.headers.get("total_records", -1)),
        "returned": len(items),
        "raw_items": items,
        "raw_text": resp.text,
    }


def fetch_project(client: PrideClient, accession: str) -> dict:
    """Fetch one project's full metadata from ``/projects/{accession}``."""
    resp = client.get(f"/projects/{seg(accession)}")
    return normalize_detail_record(resp.json())


def fetch_projects(client: PrideClient, accessions: list[str]) -> list[dict]:
    """Fetch several projects, returned sorted by accession."""
    return [fetch_project(client, acc) for acc in sorted(accessions)]
