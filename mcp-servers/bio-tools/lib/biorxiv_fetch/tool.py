"""biorxiv-fetch -- complete, count-verified retrieval from api.biorxiv.org.

Mirrors the six retrieval methods of the bioRxiv MCP connector, with the
``server`` selector (``biorxiv`` | ``medrxiv``) as a first-class argument on
every preprint route (the MCP connector's medrxiv path is unreachable through
``host.mcp`` due to a client-side parameter-name collision; this tool is the
only medRxiv route).

Route map (verified live 2026-06-08):

  /details/{server}/{doi}/na/json                       -- preprint detail (all versions)
  /details/{server}/{from}/{to}/{cursor}/json[?category=x]  -- interval listing, 30/page
  /pubs/{server}/{from}/{to}/{cursor}/json              -- published-preprint links, 100/page
  /publisher/{prefix}/{from}/{to}/{cursor}              -- publisher-filtered links
                                                            (bioRxiv only; NO /json segment)
  /funder/{server}/{from}/{to}/{ror_id}/{cursor}/json   -- funder listing
                                                            (dates BEFORE the ROR id)
  /sum/{m|y}/json                                       -- bioRxiv content statistics
  /usage/{m|y}/json                                     -- bioRxiv usage statistics

Quirks handled here:
  * ``messages[0].total`` is sometimes a string, sometimes an int.
  * detail pages are 30 records; pubs/publisher pages are 100.
  * an empty result is ``status: "no posts found"`` with HTTP 200, not an error.
  * the funder route rejects ROR ids unless the segment order is
    dates-then-ror; full ``https://ror.org/...`` URLs are normalized to the
    bare 9-character id.
"""
from __future__ import annotations

import json
import re
import urllib.parse

from .client import BiorxivApiError, BiorxivClient, IncompleteRetrieval, NotFound

SERVERS = ("biorxiv", "medrxiv")





BIORXIV_CATEGORIES = (
    "animal behavior and cognition", "biochemistry", "bioengineering",
    "bioinformatics", "biophysics", "cancer biology", "cell biology",
    "clinical trials", "developmental biology", "ecology", "epidemiology",
    "evolutionary biology", "genetics", "genomics", "immunology",
    "microbiology", "molecular biology", "neuroscience", "paleontology",
    "pathology", "pharmacology and toxicology", "physiology", "plant biology",
    "scientific communication and education", "synthetic biology",
    "systems biology", "zoology",
)

_ROR_RE = re.compile(r"^[0-9a-z]{9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DOI_RE = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9][A-Za-z0-9._;()\-]*$")
_PUBLISHER_RE = re.compile(r"^10\.\d{4,9}$")
_VOLATILE_FIELDS = frozenset({"published"})


def _check_server(server: str) -> str:
    if server not in SERVERS:
        raise ValueError(f"server must be one of {SERVERS}, got {server!r}")
    return server


def _check_date(value, name: str) -> str:
    s = str(value)
    if not _DATE_RE.match(s):
        raise ValueError(f"{name} must be 'YYYY-MM-DD', got {value!r}")
    return s


def _check_doi(doi: str) -> str:
    s = str(doi).strip()
    if not _DOI_RE.match(s):
        raise ValueError(f"not a valid preprint DOI: {doi!r}")
    return s


def _message(payload: dict) -> dict:
    msgs = payload.get("messages") or [{}]
    msg = msgs[0] if isinstance(msgs, list) else msgs
    return msg if isinstance(msg, dict) else {}


def _int(value) -> int:
    return int(str(value))


def canonicalize(obj, drop_volatile: bool = True) -> bytes:
    """Deterministic byte serialization: sorted keys, compact separators,
    ASCII-escaped, volatile fields (journal-publication linkage) dropped.
    Scientific content is never rewritten."""
    drop = _VOLATILE_FIELDS if drop_volatile else frozenset()

    def scrub(x):
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items() if k not in drop}
        if isinstance(x, list):
            return [scrub(v) for v in x]
        return x

    return json.dumps(scrub(obj), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode()


def sort_records(records: list[dict]) -> list[dict]:
    """Stable, schema-independent ordering of an unordered record collection
    (allowed canonicalization; used by the gate before comparison)."""
    return sorted(records, key=lambda r: canonicalize(r))


class BiorxivFetch:
    """The six mirrored retrieval methods, all count-verified."""

    def __init__(self, client: BiorxivClient | None = None):
        self.client = client or BiorxivClient()


    def get_preprint(self, doi: str, server: str = "biorxiv") -> dict:
        """All version records for one preprint DOI on one server."""
        _check_server(server)
        doi = _check_doi(doi)
        payload = self.client.get_json(f"/details/{server}/{doi}/na/json")
        msg = _message(payload)
        status = msg.get("status")
        if status == "no posts found":
            raise NotFound(f"DOI {doi} not found on {server}")
        if status != "ok":
            raise BiorxivApiError(f"API status {status!r} for DOI {doi} on {server}")
        versions = payload.get("collection") or []
        return {"server": server, "doi": doi, "n_versions": len(versions),
                "versions": versions}


    def _walk(self, path_for_cursor, what: str) -> tuple[int, list[dict]]:
        cursor, total, records = 0, None, []
        while True:
            payload = self.client.get_json(path_for_cursor(cursor))
            msg = _message(payload)
            status = msg.get("status")
            if status == "no posts found":
                if total is None:
                    total = 0
                break
            if status != "ok":
                raise BiorxivApiError(f"API status {status!r} during {what} at cursor {cursor}")
            page_total = _int(msg.get("total"))
            if total is None:
                total = page_total
            elif page_total != total:
                raise BiorxivApiError(
                    f"total drifted mid-walk during {what}: {total} -> {page_total}")
            page = payload.get("collection") or []
            if not page:
                break
            records.extend(page)
            cursor += len(page)
            if cursor >= total:
                break
        if len(records) != total:
            raise IncompleteRetrieval(
                f"{what}: walked {len(records)} records but API total is {total}")
        return total, records


    def list_preprints(self, server: str, date_from: str, date_to: str,
                       category: str | None = None) -> dict:
        """Complete (cursor-walked, count-verified) listing of preprints
        posted on ``server`` in the closed interval [date_from, date_to]."""
        _check_server(server)
        date_from = _check_date(date_from, "date_from")
        date_to = _check_date(date_to, "date_to")
        suffix = ""
        if category:
            suffix = "?category=" + urllib.parse.quote(
                category.strip().lower().replace(" ", "_"), safe="")
        total, records = self._walk(
            lambda c: f"/details/{server}/{date_from}/{date_to}/{c}/json{suffix}",
            f"list_preprints {server} {date_from}:{date_to}")
        return {"server": server, "interval": f"{date_from}:{date_to}",
                "category": category or "all", "total": total, "records": records}


    def published_links(self, server: str, date_from: str, date_to: str,
                        publisher_prefix: str | None = None) -> dict:
        """Preprint -> journal-article links for publication dates in the
        closed interval. ``publisher_prefix`` (a DOI prefix like ``10.1038``)
        uses the upstream /publisher route, which exists for bioRxiv only."""
        _check_server(server)
        date_from = _check_date(date_from, "date_from")
        date_to = _check_date(date_to, "date_to")
        if publisher_prefix:
            if server != "biorxiv":
                raise ValueError("the upstream /publisher route is bioRxiv-only; "
                                 "publisher_prefix cannot be combined with medrxiv")
            if not _PUBLISHER_RE.match(str(publisher_prefix)):
                raise ValueError(
                    f"publisher_prefix must be a DOI prefix like '10.1038', "
                    f"got {publisher_prefix!r}")
            total, records = self._walk(
                lambda c: f"/publisher/{publisher_prefix}/{date_from}/{date_to}/{c}",
                f"published_links publisher={publisher_prefix}")
        else:
            total, records = self._walk(
                lambda c: f"/pubs/{server}/{date_from}/{date_to}/{c}/json",
                f"published_links {server}")
        return {"server": server, "interval": f"{date_from}:{date_to}",
                "publisher_prefix": publisher_prefix, "total": total,
                "records": records}


    def by_funder(self, ror_id: str, date_from: str, date_to: str,
                  server: str = "biorxiv") -> dict:
        """Preprints acknowledging a funder (ROR id), closed date interval.
        Upstream funder coverage is sparse before ~2025-04 (probed 2026-06-08)."""
        _check_server(server)
        date_from = _check_date(date_from, "date_from")
        date_to = _check_date(date_to, "date_to")
        ror = ror_id.rsplit("/", 1)[-1].strip().lower()
        if not _ROR_RE.match(ror):
            raise ValueError(f"ROR id must be 9 chars [0-9a-z], got {ror!r}")
        total, records = self._walk(
            lambda c: f"/funder/{server}/{date_from}/{date_to}/{ror}/{c}/json",
            f"by_funder {ror} {server}")

        return {"server": server, "ror_id": ror,
                "interval": f"{date_from}:{date_to}", "total": total,
                "records": records}


    def _stats(self, route: str, interval: str, through: str | None) -> dict:
        if interval not in ("m", "y"):
            raise ValueError("interval must be 'm' (monthly) or 'y' (yearly)")
        payload = self.client.get_json(f"/{route}/{interval}/json")
        msg = _message(payload)
        if msg.get("status") != "ok":
            raise BiorxivApiError(f"API status {msg.get('status')!r} for /{route}/{interval}")
        key = next(k for k in payload if k != "messages")
        rows = payload[key]
        if through:
            def keep(row):
                if "month" in row:
                    return str(row["month"])[:7] <= through[:7]
                return _int(row["year"]) <= _int(through[:4])
            rows = [r for r in rows if keep(r)]
        return {"route": route, "interval": interval, "through": through,
                "n_rows": len(rows), "rows": rows}

    def content_stats(self, interval: str = "m", through: str | None = None) -> dict:
        """bioRxiv new/revised paper counts per month or year (cumulative
        columns included). ``through`` ('YYYY-MM' or 'YYYY') applies a
        client-side cutoff so results over closed periods are reproducible."""
        return self._stats("sum", interval, through)

    def usage_stats(self, interval: str = "m", through: str | None = None) -> dict:
        """bioRxiv abstract/full-text/PDF usage per month or year."""
        return self._stats("usage", interval, through)


def stats_runsum_violations(rows: list[dict], value_key: str, cum_key: str) -> int:
    """Count rows where cumulative[i] != cumulative[i-1] + value[i].
    An arithmetic invariant of the upstream data, independent of this tool --
    used by the accuracy gate as programmatic ground truth for statistics."""
    bad = 0
    for i in range(1, len(rows)):
        if _int(rows[i - 1][cum_key]) + _int(rows[i][value_key]) != _int(rows[i][cum_key]):
            bad += 1
    return bad
