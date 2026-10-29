"""mcp-biorxiv server — tool handlers + stdio entry point.

Tool names/schemas are served verbatim from ``schemas.json`` (captured from
the original hosted bioRxiv connector). Retrieval goes through the fleet's
``biorxiv-fetch`` package (paced/retrying client); ``marshal`` reshapes
results into the original compact-JSON output formats.

Headline fix vs the hosted connector: ``server="medrxiv"`` actually works on
every preprint route (the hosted connector's ``server`` parameter collided
with the ``host.mcp`` transport's own ``server`` argument, making medRxiv
unreachable).
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, timedelta, timezone
import datetime as _dt
from functools import lru_cache

from mcp_servers_common import Tier1Server, load_schemas
from mcp_servers_common.gate import apply_gate_tier1

from . import marshal
from .marshal import biorxiv_json

DEFAULT_WINDOW_DAYS = 60
RECENT_COUNT_WINDOW_DAYS = 90

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PUBLISHER_RE = re.compile(r"^10\.\d{4,9}$")
_ROR_RE = re.compile(r"^[0-9a-z]{9}$")


def _check_date(value, name: str) -> str:
    s = str(value)
    if not _DATE_RE.match(s):
        raise ValueError(f"{name} must be 'YYYY-MM-DD', got {value!r}")
    return s


@lru_cache(maxsize=1)
def _fetch():
    from biorxiv_fetch import BiorxivFetch
    return BiorxivFetch()


def _client():
    return _fetch().client


def _today() -> date:
    return _dt.datetime.now(timezone.utc).date()


def _normalize_doi(doi: str) -> str:
    doi = doi.strip()
    if "doi.org/" in doi:
        doi = doi.split("doi.org/", 1)[1]
    return doi


def _check_server(server: str) -> str:
    if server not in ("biorxiv", "medrxiv"):
        raise ValueError(f"server must be 'biorxiv' or 'medrxiv', got {server!r}")
    return server


def _window(args: dict) -> tuple[str, str, int | None]:
    """Resolve the original's three search methods to a date interval.

    Returns (date_from, date_to, recent_count). The original documented that
    "all searches use date ranges internally"; recent_count uses a 90-day
    window and takes the most recent N records from it.
    """
    date_from, date_to = args.get("date_from"), args.get("date_to")
    recent_days = args.get("recent_days")
    recent_count = args.get("recent_count")
    today = _today()
    if date_from and date_to:
        return (_check_date(date_from, "date_from"),
                _check_date(date_to, "date_to"), None)




    if recent_days is not None:
        return str(today - timedelta(days=int(recent_days))), str(today), None
    if recent_count is not None:
        return (str(today - timedelta(days=RECENT_COUNT_WINDOW_DAYS)),
                str(today), int(recent_count))
    return str(today - timedelta(days=DEFAULT_WINDOW_DAYS)), str(today), None


def _limit(args: dict) -> int:
    return max(1, min(100, int(args.get("limit", 10))))


def _message(payload: dict) -> dict:
    msgs = payload.get("messages") or [{}]
    return msgs[0] if isinstance(msgs, list) and msgs else {}


def _page(path_fn, cursor: int) -> tuple[list[dict], int, str]:
    """One upstream page: (records, total, status)."""
    payload = _client().get_json(path_fn(cursor))
    msg = _message(payload)
    status = msg.get("status")
    if status == "no posts found":
        return [], 0, status
    if status != "ok":
        raise RuntimeError(f"bioRxiv API status {status!r} at cursor {cursor}")
    total = int(str(msg.get("total", 0) or 0))
    return payload.get("collection") or [], total, status


def _collect(path_fn, start: int, needed: int) -> tuple[list[dict], int]:
    """Walk pages from ``start`` until ``needed`` records (or exhaustion)."""
    records: list[dict] = []
    total = 0
    cursor = start
    while len(records) < needed:
        page, total, status = _page(path_fn, cursor)
        if not page:
            break
        records.extend(page)
        cursor += len(page)
        if cursor >= total:
            break
    return records[:needed], total


def _search_window(path_fn, cursor: int, limit: int,
                   recent_count: int | None) -> tuple[list[dict], int]:
    if recent_count is None:
        return _collect(path_fn, cursor, limit)

    _, total, status = _page(path_fn, 0)
    if status == "no posts found" or total == 0:
        return [], 0
    if recent_count == 0:



        return [], total
    start = max(total - recent_count, 0) + cursor
    return _collect(path_fn, start, min(limit, recent_count))



def get_categories(args: dict) -> str:
    from biorxiv_fetch.tool import BIORXIV_CATEGORIES
    return biorxiv_json(marshal.categories_response(BIORXIV_CATEGORIES))


def get_preprint(args: dict) -> str:
    try:
        server = _check_server(args.get("server", "biorxiv"))
        doi = _normalize_doi(args["doi"])
        result = _fetch().get_preprint(doi, server=server)
        return biorxiv_json(marshal.preprint_response(result["versions"], server))
    except Exception as exc:
        return biorxiv_json(marshal.preprint_error(str(exc)))


def search_preprints(args: dict) -> str:
    cursor = int(args.get("cursor", 0))
    try:
        server = _check_server(args.get("server", "biorxiv"))
        date_from, date_to, recent_count = _window(args)
        suffix = ""
        category = args.get("category")
        if category:
            suffix = "?category=" + urllib.parse.quote(
                category.strip().lower().replace(" ", "_"), safe="")
        records, total = _search_window(
            lambda c: f"/details/{server}/{date_from}/{date_to}/{c}/json{suffix}",
            cursor, _limit(args), recent_count)
        return biorxiv_json(marshal.search_response(records, cursor, total))
    except Exception as exc:
        return biorxiv_json(marshal.search_error(str(exc), cursor))


def search_published_preprints(args: dict) -> str:
    cursor = int(args.get("cursor", 0))
    try:
        server = _check_server(args.get("server", "biorxiv"))
        publisher = args.get("publisher")
        date_from, date_to, recent_count = _window(args)
        if publisher:
            if server != "biorxiv":
                raise ValueError(
                    "the upstream /publisher route is bioRxiv-only; "
                    "publisher cannot be combined with server='medrxiv'")
            if not _PUBLISHER_RE.match(str(publisher)):
                raise ValueError(
                    f"publisher must be a DOI prefix like '10.1038', "
                    f"got {publisher!r}")
            path_fn = (lambda c:
                       f"/publisher/{publisher}/{date_from}/{date_to}/{c}")
        else:
            path_fn = (lambda c:
                       f"/pubs/{server}/{date_from}/{date_to}/{c}/json")
        records, total = _search_window(path_fn, cursor, _limit(args),
                                        recent_count)
        return biorxiv_json(marshal.published_response(
            records, cursor, total, bool(args.get("include_details", True))))
    except Exception as exc:
        return biorxiv_json(marshal.search_error(str(exc), cursor))


def search_by_funder(args: dict) -> str:
    cursor = int(args.get("cursor", 0))
    try:
        server = _check_server(args.get("server", "biorxiv"))
        ror = str(args["funder_ror_id"]).rsplit("/", 1)[-1].strip().lower()
        if not _ROR_RE.match(ror):
            raise ValueError(f"ROR id must be 9 chars [0-9a-z], got {ror!r}")
        date_from = _check_date(args["date_from"], "date_from")
        date_to = _check_date(args["date_to"], "date_to")
        suffix = ""
        category = args.get("category")
        if category:
            suffix = "?category=" + urllib.parse.quote(
                category.strip().lower().replace(" ", "_"), safe="")
        records, total = _collect(
            lambda c: f"/funder/{server}/{date_from}/{date_to}/{ror}/{c}/json{suffix}",
            cursor, _limit(args))
        return biorxiv_json(marshal.search_response(records, cursor, total))
    except Exception as exc:
        return biorxiv_json(marshal.search_error(str(exc), cursor))


_INTERVALS = {"monthly": "m", "yearly": "y"}


def _interval(args: dict) -> str:



    key = args.get("interval") or "monthly"
    if key not in _INTERVALS:
        raise ValueError(
            f"Invalid interval: {key}. Must be one of: monthly, yearly")
    return _INTERVALS[key]


def get_content_statistics(args: dict) -> str:
    try:
        result = _fetch().content_stats(interval=_interval(args))
        return biorxiv_json(marshal.content_stats_response(result["rows"]))
    except Exception as exc:
        return biorxiv_json(marshal.stats_error(str(exc)))


def get_usage_statistics(args: dict) -> str:
    try:
        result = _fetch().usage_stats(interval=_interval(args))
        return biorxiv_json(marshal.usage_stats_response(result["rows"]))
    except Exception as exc:
        return biorxiv_json(marshal.stats_error(str(exc)))


HANDLERS = {
    "get_categories": get_categories,
    "get_content_statistics": get_content_statistics,
    "get_preprint": get_preprint,
    "get_usage_statistics": get_usage_statistics,
    "search_by_funder": search_by_funder,
    "search_preprints": search_preprints,
    "search_published_preprints": search_published_preprints,
}


def build_server() -> Tier1Server:
    return Tier1Server("bioRxiv", load_schemas(__package__), HANDLERS)


def main() -> None:



    t1 = build_server()
    apply_gate_tier1(t1)
    t1.run()


if __name__ == "__main__":
    main()
