"""Complete annotation retrieval (downloadSearch, with paged-search fallback)."""
from __future__ import annotations

from .client import QuickGOClient
from .queries import AnnotationQuery
from .terms import fetch_term_metadata


DOWNLOAD_LIMIT_MAX = 50000
SEARCH_PAGE_LIMIT_MAX = 200
DEFAULT_SEARCH_PAGE_SIZE = 25


TSV_COLUMNS = [
    "GENE PRODUCT DB", "GENE PRODUCT ID", "SYMBOL", "QUALIFIER", "GO TERM",
    "GO ASPECT", "ECO ID", "GO EVIDENCE CODE", "REFERENCE", "WITH/FROM",
    "TAXON ID", "ASSIGNED BY", "ANNOTATION EXTENSION", "DATE",
]

_ASPECT_EXPAND = {"P": "biological_process", "F": "molecular_function",
                  "C": "cellular_component"}


RECORD_FIELDS = [
    "gene_product_db", "gene_product_id", "symbol", "qualifier", "go_id",
    "go_aspect", "eco_id", "go_evidence", "reference", "with_from",
    "taxon_id", "assigned_by", "annotation_extension", "date",
]


def _record_from_tsv_row(row: list[str]) -> dict:
    rec = {
        "gene_product_db": row[0],
        "gene_product_id": row[1],
        "symbol": row[2],
        "qualifier": row[3],
        "go_id": row[4],
        "go_aspect": _ASPECT_EXPAND.get(row[5], row[5]),
        "eco_id": row[6],
        "go_evidence": row[7],
        "reference": row[8],
        "with_from": row[9],
        "taxon_id": int(row[10]) if row[10] else None,
        "assigned_by": row[11],
        "annotation_extension": row[12],
        "date": row[13],
    }
    return rec


def parse_download_tsv(text: str) -> list[dict]:
    """Parse a /annotation/downloadSearch TSV payload into structured records."""
    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    if header != TSV_COLUMNS:
        raise ValueError(f"Unexpected downloadSearch TSV header: {header}")
    records = []
    for line in lines[1:]:
        if not line:
            continue
        row = line.split("\t")
        if len(row) != len(TSV_COLUMNS):
            raise ValueError(f"Malformed TSV row ({len(row)} fields): {line[:200]}")
        records.append(_record_from_tsv_row(row))
    return records


def sort_records(records: list[dict]) -> list[dict]:
    """Deterministic total order over annotation records (all fields)."""
    def key(r: dict):
        return tuple("" if r[f] is None else str(r[f]) for f in RECORD_FIELDS)
    return sorted(records, key=key)


def count_annotations(client: QuickGOClient, query: AnnotationQuery) -> int:
    """The API's own total (numberOfHits) for a query, via /annotation/search&limit=1."""
    params = query.to_params() | {"limit": "1"}
    payload = client.get_json("/annotation/search", params)
    return int(payload["numberOfHits"])


def _fetch_via_download(client: QuickGOClient, query: AnnotationQuery) -> list[dict]:
    params = query.to_params() | {"downloadLimit": str(DOWNLOAD_LIMIT_MAX)}
    tsv = client.get_tsv("/annotation/downloadSearch", params)
    return parse_download_tsv(tsv)


def _fetch_via_paged_search(client: QuickGOClient, query: AnnotationQuery,
                            total: int) -> list[dict]:
    """Fallback for result sets above the downloadSearch cap: page /annotation/search."""
    records: list[dict] = []
    page = 1
    while len(records) < total:
        params = query.to_params() | {"limit": str(SEARCH_PAGE_LIMIT_MAX), "page": str(page)}
        payload = client.get_json("/annotation/search", params)
        results = payload.get("results", [])
        if not results:
            break
        for r in results:
            records.append({
                "gene_product_db": r["geneProductId"].split(":", 1)[0],
                "gene_product_id": r["geneProductId"].split(":", 1)[-1],
                "symbol": r.get("symbol") or "",
                "qualifier": r.get("qualifier") or "",
                "go_id": r["goId"],
                "go_aspect": r.get("goAspect") or "",
                "eco_id": r.get("evidenceCode") or "",
                "go_evidence": r.get("goEvidence") or "",
                "reference": r.get("reference") or "",
                "with_from": _flatten_with_from(r.get("withFrom")),
                "taxon_id": r.get("taxonId"),
                "assigned_by": r.get("assignedBy") or "",
                "annotation_extension": _flatten_extensions(r.get("extensions")),
                "date": r.get("date") or "",
            })
        page += 1
    return records


def _flatten_with_from(with_from) -> str:
    if not with_from:
        return ""
    groups = []
    for grp in with_from:
        xrefs = grp.get("connectedXrefs") or []
        groups.append(",".join(f"{x['db']}:{x['id']}" for x in xrefs))
    return "|".join(groups)


def _flatten_extensions(extensions) -> str:
    if not extensions:
        return ""
    groups = []
    for grp in extensions:
        xrefs = grp.get("connectedXrefs") or []
        groups.append(",".join(f"{x.get('relation','')}({x['db']}:{x['id']})" for x in xrefs))
    return "|".join(groups)


def fetch_annotations(client: QuickGOClient, query: AnnotationQuery,
                      verify: bool = True) -> dict:
    """Retrieve the COMPLETE annotation set for one query.

    Returns {"query": <label>, "total_items": <API numberOfHits>,
             "n_records": <len(records)>, "complete": bool, "records": [...]}.
    Records are deterministically sorted. When `verify` is True (default) the
    API's own total is fetched and compared to the number of records retrieved.
    """
    total = count_annotations(client, query) if verify else None
    if total is not None and total > DOWNLOAD_LIMIT_MAX:
        records = _fetch_via_paged_search(client, query, total)
    else:
        records = _fetch_via_download(client, query)
        if total is None and len(records) >= DOWNLOAD_LIMIT_MAX:

            total = count_annotations(client, query)
            if total > DOWNLOAD_LIMIT_MAX:
                records = _fetch_via_paged_search(client, query, total)
    records = sort_records(records)
    out = {
        "query": query.label(),
        "params": query.to_params(),
        "total_items": total,
        "n_records": len(records),
        "complete": (total is None) or (len(records) == total),
        "records": records,
    }
    return out


def fetch_annotation_set(client: QuickGOClient, queries: list[AnnotationQuery],
                         hydrate_term_names: bool = True, verify: bool = True) -> dict:
    """Retrieve complete annotation sets for many queries, optionally hydrating
    each record with the GO term name/aspect/obsolete flag from the ontology
    endpoint (one batched lookup over the distinct GO IDs)."""
    results = [fetch_annotations(client, q, verify=verify) for q in queries]
    term_index: dict[str, dict] = {}
    if hydrate_term_names:
        distinct_ids = sorted({r["go_id"] for res in results for r in res["records"]})
        term_index = fetch_term_metadata(client, distinct_ids)
        for res in results:
            for rec in res["records"]:
                meta = term_index.get(rec["go_id"])
                rec["go_name"] = meta["name"] if meta else None
                rec["go_is_obsolete"] = meta["is_obsolete"] if meta else None
    return {"queries": results, "terms": term_index}
