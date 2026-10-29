"""Reshape biorxiv-fetch payloads into the ORIGINAL bioRxiv connector's
output formats (see mcp-servers/_snapshots/original_outputs/mcp-biorxiv/).

The original serialized compact JSON (no spaces), so ``biorxiv_json`` uses
``separators=(",", ":")`` rather than the pretty form other connectors used.
"""

from __future__ import annotations

import json

PREVIEW_CHARS = 200

SERVER_DOMAINS = {"biorxiv": "www.biorxiv.org", "medrxiv": "www.medrxiv.org"}


def biorxiv_json(obj: object) -> str:
    """Serialize like the original bioRxiv connector (compact, UTF-8 kept)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _preview(abstract: object) -> str | None:
    if not isinstance(abstract, str):
        return None
    if len(abstract) > PREVIEW_CHARS:
        return abstract[:PREVIEW_CHARS] + "..."
    return abstract



def categories_response(names) -> dict:
    return {
        "success": True,
        "categories": [
            {"name": n, "api_format": n.replace(" ", "_"), "description": None}
            for n in names
        ],
        "error": None,
    }



def preprint_response(versions: list[dict], server: str) -> dict:
    """Original emitted a single version record. It used the FIRST (oldest)
    version; we use the latest (see README: deliberate behavior change) and
    add ``n_versions`` as a new field."""
    rec = versions[-1]
    doi = rec.get("doi")
    version = rec.get("version")
    domain = SERVER_DOMAINS.get(server, SERVER_DOMAINS["biorxiv"])
    funder = rec.get("funder")
    return {
        "success": True,
        "preprint": {
            "doi": doi,
            "title": rec.get("title"),
            "authors": rec.get("authors"),
            "author_corresponding": rec.get("author_corresponding"),
            "author_corresponding_institution":
                rec.get("author_corresponding_institution"),
            "date": rec.get("date"),
            "version": version,
            "type": rec.get("type"),
            "category": rec.get("category"),
            "license": rec.get("license"),
            "abstract": rec.get("abstract"),
            "jatsxml": rec.get("jatsxml"),
            "funding": None if funder in (None, "NA") else funder,
            "published_doi": rec.get("published", "NA"),
            "server": rec.get("server"),
            "pdf_url": f"https://{domain}/content/{doi}v{version}.full.pdf",
            "web_url": f"https://{domain}/content/{doi}v{version}",

            "n_versions": len(versions),
        },
        "error": None,
    }


def preprint_error(message: str) -> dict:
    return {"success": False, "preprint": None, "error": message}



def preprint_summary(rec: dict) -> dict:
    return {
        "doi": rec.get("doi"),
        "title": rec.get("title"),
        "authors": rec.get("authors"),
        "date": rec.get("date"),
        "category": rec.get("category"),
        "version": rec.get("version"),
        "abstract_preview": _preview(rec.get("abstract")),
    }


def search_response(records: list[dict], cursor: int,
                    total: int | None) -> dict:
    """Original always emitted ``"total": 0``; we report the upstream total
    when the route provides one (README: deliberate behavior change — type
    unchanged). ``None`` means the upstream route carries NO total
    (``/funder/…`` — 06-25 probe item 7): callers use ``count < limit`` to
    detect end-of-stream. Never ``0`` with a non-empty page."""
    results = [preprint_summary(r) for r in records]


    t = None if (not total and results) else total
    return {"success": True, "results": results, "cursor": cursor,
            "count": len(results), "total": t, "error": None}


def search_error(message: str, cursor: int) -> dict:
    return {"success": False, "results": [], "cursor": cursor,
            "count": 0, "total": 0, "error": message}



_PUBLISHED_SUMMARY_KEYS = (
    "published_doi", "published_journal", "preprint_platform",
    "preprint_title", "preprint_category", "preprint_date", "published_date",
)


def published_record(rec: dict, include_details: bool) -> dict:
    out = {"biorxiv_doi": rec.get("preprint_doi", rec.get("biorxiv_doi"))}
    if include_details:
        for k, v in rec.items():
            if k not in ("preprint_doi", "biorxiv_doi"):
                out[k] = v
    else:
        for k in _PUBLISHED_SUMMARY_KEYS:
            if k in rec:
                out[k] = rec[k]
    return out


def published_response(records: list[dict], cursor: int, total: int | None,
                       include_details: bool) -> dict:
    results = [published_record(r, include_details) for r in records]
    t = None if (not total and results) else total
    return {"success": True, "results": results, "cursor": cursor,
            "count": len(results), "total": t, "error": None}



def _int(value) -> int:
    return int(str(value))


def content_stats_response(rows: list[dict]) -> dict:
    """Original row order: period, new_papers, new_papers_cumulative,
    revised_papers, preprint_date (always null — replicated for shape
    parity), revised_papers_cumulative. Monthly rows carry ``month``
    (YYYY-MM), yearly rows carry ``year``."""
    results = []
    for r in rows:
        out = {}
        if "month" in r:
            out["month"] = str(r["month"])
        else:
            out["year"] = _int(r["year"])
        out["new_papers"] = _int(r["new_papers"])
        out["new_papers_cumulative"] = _int(r["new_papers_cumulative"])
        out["revised_papers"] = _int(r["revised_papers"])
        out["preprint_date"] = None
        out["revised_papers_cumulative"] = _int(r["revised_papers_cumulative"])
        results.append(out)
    return {"success": True, "results": results, "error": None}


def usage_stats_response(rows: list[dict]) -> dict:
    results = []
    for r in rows:
        out = {}
        if "month" in r:
            out["month"] = str(r["month"])
        else:
            out["year"] = str(r["year"])
        for k in ("abstract_views", "full_text_views", "pdf_downloads",
                  "abstract_cumulative", "full_text_cumulative",
                  "pdf_cumulative"):
            out[k] = _int(r[k])
        results.append(out)
    return {"success": True, "results": results, "error": None}


def stats_error(message: str) -> dict:
    return {"success": False, "results": [], "error": message}
