"""ArxivFetch — search and batch metadata retrieval over the arXiv Atom API.

Marshalling: Atom entries -> flat dicts (id/version split, abstract,
categories, DOI, journal ref, PDF link). Listings are honest: the feed's
``opensearch:totalResults`` is always returned as ``api_total`` and
``records_truncated`` flags a capped page. Malformed queries — which arXiv
reports as an HTTP-200 feed containing a single ``api/errors`` entry — are
raised as errors instead of being returned as fake results.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .client import ArxivClient, ArxivApiError

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

MAX_RESULTS_CEILING = 100
MAX_IDS_PER_FETCH = 100

SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}
SORT_ORDER = {"ascending", "descending"}

_ID_VERSION = re.compile(r"^(?P<id>.+?)(?:v(?P<version>\d+))?$")
_DATE_DIGITS = re.compile(r"^\d{8}$")


_ARXIV_ID = re.compile(
    r"^(\d{4}\.\d{4,5}|[a-z][a-z-]*(\.[A-Za-z-]+)?/\d{7})(v\d+)?$")


def normalize_arxiv_id(raw: str) -> str:
    """Normalize an arXiv reference to a bare (possibly versioned) ID.

    Accepts ``2103.14030``, ``2103.14030v2``, abs/pdf URLs, and old-style
    IDs like ``q-bio/0601001``.
    """
    s = raw.strip()
    s = re.sub(r"^https?://(export\.)?arxiv\.org/(abs|pdf)/", "", s)
    s = re.sub(r"^arxiv:", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.pdf$", "", s)
    if not s:
        raise ValueError(f"empty arXiv id from {raw!r}")
    return s


def _entry_text(entry: ET.Element, tag: str) -> str | None:
    val = entry.findtext(tag, namespaces=NS)
    if val is None:
        return None
    return re.sub(r"\s+", " ", val).strip() or None


def parse_entry(entry: ET.Element) -> dict:
    """One Atom <entry> -> flat record dict.

    Entries without an ``atom:id`` (blank/withdrawn stubs) keep their
    metadata with null id fields — never a crash, never a silent drop.
    """
    abs_url = entry.findtext("atom:id", namespaces=NS) or ""
    versioned = abs_url.rsplit("/abs/", 1)[-1]
    m = _ID_VERSION.match(versioned) if versioned else None
    arxiv_id = m.group("id") if m else None
    version = m.group("version") if m else None
    pdf_url = None
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf" or \
                link.get("type") == "application/pdf":
            pdf_url = link.get("href")
    primary = entry.find("arxiv:primary_category", NS)
    return {
        "arxiv_id": arxiv_id,
        "version": int(version) if version else None,
        "id_versioned": versioned or None,
        "title": _entry_text(entry, "atom:title"),
        "abstract": _entry_text(entry, "atom:summary"),
        "authors": [a.findtext("atom:name", namespaces=NS)
                    for a in entry.findall("atom:author", NS)],
        "published": entry.findtext("atom:published", namespaces=NS),
        "updated": entry.findtext("atom:updated", namespaces=NS),
        "primary_category": primary.get("term") if primary is not None
        else None,
        "categories": [c.get("term")
                       for c in entry.findall("atom:category", NS)],
        "doi": _entry_text(entry, "arxiv:doi"),
        "journal_ref": _entry_text(entry, "arxiv:journal_ref"),
        "comment": _entry_text(entry, "arxiv:comment"),
        "abs_url": abs_url or None,
        "pdf_url": pdf_url,
    }


def parse_feed(root: ET.Element) -> dict:
    """Atom feed root -> {api_total, start_index, records}.

    Raises ArxivApiError when the feed is arXiv's HTTP-200 error envelope
    (single entry whose id lives under ``/api/errors``).
    """
    entries = root.findall("atom:entry", NS)
    if len(entries) == 1:
        eid = entries[0].findtext("atom:id", namespaces=NS) or ""
        if "/api/errors" in eid:
            msg = entries[0].findtext("atom:summary", namespaces=NS) or \
                entries[0].findtext("atom:title", namespaces=NS) or "error"
            raise ArxivApiError(f"arXiv API error: {msg.strip()}")
    total = root.findtext("opensearch:totalResults", namespaces=NS)
    start = root.findtext("opensearch:startIndex", namespaces=NS)
    return {
        "api_total": int(total) if total is not None else None,
        "start_index": int(start) if start is not None else None,
        "records": [parse_entry(e) for e in entries],
    }


class ArxivFetch:
    def __init__(self, client: ArxivClient | None = None):
        self.client = client or ArxivClient()

    def search(self, query: str | None = None, category: str | None = None,
               date_from: str | None = None, date_to: str | None = None,
               start: int = 0, max_results: int = 25,
               sort_by: str = "relevance",
               sort_order: str = "descending") -> dict:
        if sort_by not in SORT_BY:
            raise ValueError(f"sort_by must be one of {sorted(SORT_BY)}")
        if sort_order not in SORT_ORDER:
            raise ValueError(
                f"sort_order must be one of {sorted(SORT_ORDER)}")
        clauses: list[str] = []
        if query:
            clauses.append(f"({query})" if " AND " in query or " OR " in
                           query or " ANDNOT " in query else query)
        if category:
            clauses.append(f"cat:{category}")
        if date_from or date_to:
            lo = _date_stamp(date_from, "0000") if date_from else \
                "199101010000"
            hi = _date_stamp(date_to, "2359") if date_to else "299912312359"
            clauses.append(f"submittedDate:[{lo} TO {hi}]")
        if not clauses:
            raise ValueError("pass a query, a category, and/or a date range")
        n = max(1, min(int(max_results), MAX_RESULTS_CEILING))
        root = self.client.query({
            "search_query": " AND ".join(clauses),
            "start": max(0, int(start)),
            "max_results": n,
            "sortBy": sort_by,
            "sortOrder": sort_order,
        })
        out = parse_feed(root)
        total = out["api_total"] or 0
        returned = len(out["records"])
        start_idx = out["start_index"] or 0
        return {
            "search_query": " AND ".join(clauses),
            "api_total": total,
            "start_index": start_idx,
            "n_records_returned": returned,
            "records_truncated": start_idx + returned < total,
            "sort_by": sort_by, "sort_order": sort_order,
            "records": out["records"],
        }

    def get_papers(self, arxiv_ids: list[str]) -> dict:
        requested = [i for i in arxiv_ids if i.strip()]
        if not requested:
            raise ValueError("pass at least one arXiv id")
        if len(requested) > MAX_IDS_PER_FETCH:
            raise ValueError(f"at most {MAX_IDS_PER_FETCH} ids per call "
                             f"(got {len(requested)})")



        ids, not_found = [], []
        duplicates: list[dict] = []
        for raw in requested:
            try:
                norm = normalize_arxiv_id(raw)
            except ValueError:
                not_found.append(raw.strip())
                continue
            (ids if _ARXIV_ID.match(norm) else not_found).append(norm)
        records: list[dict] = []
        if ids:
            root = self.client.query({
                "id_list": ",".join(ids), "max_results": len(ids)})
            out = parse_feed(root)


            by_id: dict[str, dict] = {}
            for rec in out["records"]:
                if rec["arxiv_id"] is None:
                    continue
                by_id.setdefault(rec["arxiv_id"], rec)
                by_id.setdefault(rec["id_versioned"], rec)
            seen: dict[int, str] = {}
            for req in ids:
                rec = by_id.get(req) or by_id.get(re.sub(r"v\d+$", "", req))
                if rec is None:
                    not_found.append(req)
                elif id(rec) not in seen:
                    seen[id(rec)] = req
                    records.append(rec)
                else:




                    duplicates.append(
                        {"requested": req, "resolved_as": seen[id(rec)]})
        return {"n_requested": len(requested), "n_found": len(records),
                "duplicates": duplicates,
                "not_found": not_found, "records": records}


def _date_stamp(date: str, hhmm: str) -> str:
    """'YYYY-MM-DD' or 'YYYYMMDD' -> arXiv submittedDate stamp."""
    digits = date.replace("-", "").strip()
    if not _DATE_DIGITS.match(digits):
        raise ValueError(
            f"bad date {date!r} — pass YYYY-MM-DD (e.g. 2024-01-31)")
    return digits + hhmm
