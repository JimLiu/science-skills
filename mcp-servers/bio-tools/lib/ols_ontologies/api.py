"""High-level operations: per-ID metadata fetch, full catalogue listing,
term-count verification."""
from __future__ import annotations

from typing import Iterable, Optional

from mcp_servers_common.pathseg import seg

from .client import OLSClient
from .records import parse_ontology_record, strip_links


def fetch_ontologies(ontology_ids: Iterable[str], client: Optional[OLSClient] = None) -> dict:
    """Fetch structured metadata records for a list of ontology IDs via
    GET /ontologies/{id}. Output order == input order. Unknown IDs are
    reported explicitly in `not_found`, never silently dropped."""
    own = client is None
    client = client or OLSClient()
    records, not_found, raw_by_id = [], [], {}
    try:
        for oid in ontology_ids:
            raw = client.get_json(f"/ontologies/{seg(oid)}")
            if raw is None:
                not_found.append(oid)
                continue
            raw_by_id[oid] = strip_links(raw)
            records.append(parse_ontology_record(raw))
    finally:
        if own:
            client.close()
    return {"records": records, "not_found": not_found, "raw": raw_by_id}


def list_catalogue(client: Optional[OLSClient] = None, page_size: int = 500) -> dict:
    """Retrieve the complete ontology catalogue via GET /ontologies with
    explicit pagination, verified against page.totalElements.

    Returns records sorted by ontology_id (the API's own deterministic order),
    the reported total, and a `complete` flag (len(records) == totalElements
    and no duplicate IDs)."""
    own = client is None
    client = client or OLSClient()
    records, raws = [], []
    try:
        page_number = 0
        total_elements = None
        total_pages = None
        while True:
            data = client.get_json("/ontologies", params={"size": page_size, "page": page_number})
            if data is None:
                raise RuntimeError("catalogue listing returned 404")
            page = data.get("page", {})
            total_elements = page.get("totalElements")
            total_pages = page.get("totalPages")
            items = (data.get("_embedded") or {}).get("ontologies", [])
            for raw in items:
                raws.append(strip_links(raw))
                records.append(parse_ontology_record(raw))
            page_number += 1
            if total_pages is None or page_number >= total_pages:
                break
    finally:
        if own:
            client.close()
    order = sorted(range(len(records)), key=lambda i: records[i]["ontology_id"] or "")
    records = [records[i] for i in order]
    raws = [raws[i] for i in order]
    ids = [r["ontology_id"] for r in records]
    complete = (total_elements is not None
                and len(records) == total_elements
                and len(ids) == len(set(ids)))
    return {
        "records": records,
        "raw": raws,
        "total_elements": total_elements,
        "pages_fetched": page_number,
        "complete": complete,
    }


def verify_term_counts(ontology_ids: Iterable[str], client: Optional[OLSClient] = None,
                       record_counts: Optional[dict] = None) -> dict:
    """Cross-check the ontology record's numberOfTerms against the live terms
    endpoint total (GET /ontologies/{id}/terms?size=1 -> page.totalElements).

    `record_counts` may carry already-fetched {ontology_id: num_terms} to avoid
    re-fetching the record; otherwise the record is fetched here."""
    own = client is None
    client = client or OLSClient()
    checks = []
    try:
        for oid in ontology_ids:
            if record_counts is not None and oid in record_counts:
                rec_count = record_counts[oid]
            else:
                raw = client.get_json(f"/ontologies/{seg(oid)}")
                rec_count = None if raw is None else raw.get("numberOfTerms")
            tpage = client.get_json(
                f"/ontologies/{seg(oid)}/terms",
                params={"size": 1})
            terms_total = None if tpage is None else (tpage.get("page") or {}).get("totalElements")
            checks.append({
                "ontology_id": oid,
                "record_num_terms": rec_count,
                "terms_endpoint_total": terms_total,
                "match": rec_count is not None and rec_count == terms_total,
            })
    finally:
        if own:
            client.close()
    return {"checks": checks, "n_match": sum(c["match"] for c in checks), "n_total": len(checks)}
