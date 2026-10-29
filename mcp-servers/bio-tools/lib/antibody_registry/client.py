"""Antibody Registry (antibodyregistry.org) client.

Routes (from the API's own OpenAPI spec at /api/openapi.json, resolved 2026-06-08):

* ``GET /api/fts-antibodies?q=&page=&size=``   -- full-text search (1-based pages).
* ``GET /api/antibodies/{ab_id}``              -- per-antibody detail; returns a LIST
  (an accession can map to >1 curated record, e.g. multi-vendor duplicates).
* ``GET /api/antibodies?page=&size=``          -- unfiltered global listing. This route
  has NO search parameter (search=/q=/name= are silently ignored upstream).
* ``GET /api/datainfo``                        -- registry size + last update date.
* ``GET /api/vendors``                         -- vendor list.

``POST /api/search/antibodies`` accepts a FilterRequest whose ``search`` term works
(identical results to fts-antibodies), but every column filter variant
(contains/equals on any key) returned HTTP 500 upstream at build time, so catalog
lookup is implemented as full-text search + client-side exact matching.

Anonymous depth cap (measured 2026-06-08): fts-antibodies returns HTTP 401 for any
row beyond offset 500 (``page * size > 500``). Result sets larger than 500 rows
cannot be fully retrieved without authentication; the client stops at the cap and
reports ``anonymous_limit_hit: True`` instead of raising.
"""

from __future__ import annotations

import re
import time
from typing import Any

import requests

BASE_URL = "https://www.antibodyregistry.org/api"
USER_AGENT = "bio-tools/antibody-registry (anthropic-experimental)"



ANON_ROW_LIMIT = 500



VOLATILE_FIELDS = frozenset(
    {"curateTime", "lastEditTime", "ix", "showLink", "feedback", "numOfCitation"}
)

_RRID_RE = re.compile(r"^(?:RRID:)?AB_(\d+)$", re.IGNORECASE)


def parse_ab_id(value: int | str) -> int:
    """Accept 3643095, "3643095", "AB_3643095" or "RRID:AB_3643095"."""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    m = _RRID_RE.match(s)
    if m:
        return int(m.group(1))
    if s.isdigit():
        return int(s)
    raise ValueError(f"not a valid antibody id / RRID: {value!r}")


def to_rrid(ab_id: int) -> str:
    return f"AB_{ab_id}"


class AntibodyRegistryClient:
    """Polite client for the SciCrunch Antibody Registry API (<=2 req/s)."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        min_interval_s: float = 0.5,
        timeout_s: float = 60.0,
        session: requests.Session | None = None,
        keep_volatile: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.keep_volatile = keep_volatile
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self._last_request_t = 0.0
        self.request_count = 0
        self.bytes_downloaded = 0


    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout_s
        )
        self._last_request_t = time.monotonic()
        self.request_count += 1
        self.bytes_downloaded += len(resp.content)
        resp.raise_for_status()
        return resp.json()


    def _norm(self, record: dict[str, Any]) -> dict[str, Any]:
        if self.keep_volatile:
            return dict(sorted(record.items()))
        return {k: v for k, v in sorted(record.items()) if k not in VOLATILE_FIELDS}


    def search_antibodies(
        self,
        q: str,
        page: int | None = None,
        size: int = 100,
        max_records: int = 5000,
    ) -> dict[str, Any]:
        """Full-text search via GET /api/fts-antibodies.

        Token-based matching against name/target/catalog text (upstream semantics:
        "TP53" and "p53" are different queries). Pages are 1-based; ``page=None``
        walks ALL pages (up to ``max_records``) and verifies the row count against
        the API's own ``totalElements``.

        Note: ``totalElements`` counts index ROWS, not unique antibodies -- the
        upstream index contains deterministic duplicate rows for some accessions
        (multiplicities mirror the per-ID route's record count).

        Anonymous depth cap: rows beyond offset ``ANON_ROW_LIMIT`` (500) return
        HTTP 401 upstream. The walk stops at the cap and sets
        ``anonymous_limit_hit: True`` (with ``complete: False``).
        """
        if not q or not q.strip():
            raise ValueError("query must be non-empty")
        if page is not None:
            if page * size > ANON_ROW_LIMIT:
                raise ValueError(
                    f"page*size={page * size} exceeds the anonymous row limit "
                    f"({ANON_ROW_LIMIT}); upstream returns HTTP 401 beyond it"
                )
            data = self._get("/fts-antibodies", {"q": q, "page": page, "size": size})
            items = [self._norm(r) for r in data.get("items", [])]
            return {
                "query": q,
                "page": page,
                "total_elements": data.get("totalElements"),
                "retrieved": len(items),
                "complete": len(items) == data.get("totalElements"),
                "items": items,
            }
        items: list[dict[str, Any]] = []
        pg, total = 1, None
        limit_hit = False
        while True:
            if pg * size > ANON_ROW_LIMIT:
                limit_hit = True
                break
            data = self._get("/fts-antibodies", {"q": q, "page": pg, "size": size})
            total = data.get("totalElements")
            batch = data.get("items", [])
            if not batch:
                break
            items.extend(self._norm(r) for r in batch)
            if len(items) >= min(total or 0, max_records):
                break
            pg += 1
        truncated = total is not None and len(items) < total
        return {
            "query": q,
            "total_elements": total,
            "retrieved": len(items),
            "unique_ab_ids": len({r.get("abId") for r in items}),
            "complete": not truncated,
            "truncated_at_max_records": truncated and not limit_hit,
            "anonymous_limit_hit": limit_hit,
            "items": items,
        }

    def get_antibody(self, ab_id: int | str) -> dict[str, Any]:
        """Per-antibody detail via GET /api/antibodies/{id}.

        Accepts a plain integer, "AB_<id>" or "RRID:AB_<id>". Returns ALL records
        for the accession (the upstream route is list-valued; some accessions have
        several curated records). A nonexistent id yields ``record_count == 0``
        (upstream returns 200 with an empty list, not 404).
        """
        num = parse_ab_id(ab_id)
        data = self._get(f"/antibodies/{num}")
        records = data if isinstance(data, list) else [data]
        return {
            "ab_id": num,
            "rrid": to_rrid(num),
            "record_count": len(records),
            "records": [self._norm(r) for r in records],
        }

    def by_catalog(
        self, catalog_num: str, vendor: str | None = None, size: int = 100
    ) -> dict[str, Any]:
        """Find antibodies by vendor catalog number (exact, case-insensitive).

        Implemented as full-text search + client-side exact match on
        ``catalogNum`` (or membership in the ``catAlt`` alternatives string),
        because the upstream column-filter route (POST /api/search/antibodies
        contains/equals) returns HTTP 500 for every key (verified 2026-06-08).
        """
        if not catalog_num or not catalog_num.strip():
            raise ValueError("catalog_num must be non-empty")
        want = catalog_num.strip().casefold()
        res = self.search_antibodies(catalog_num.strip(), size=size)
        matches = []
        for r in res["items"]:
            cat = (r.get("catalogNum") or "").strip().casefold()
            alts = {
                a.strip().casefold()
                for a in re.split(r"[,;]", r.get("catAlt") or "")
                if a.strip()
            }
            if want == cat or want in alts:
                if vendor is None or (r.get("vendorName") or "").strip().casefold() == \
                        vendor.strip().casefold():
                    matches.append(r)
        return {
            "catalog_num": catalog_num,
            "vendor": vendor,
            "match_count": len(matches),
            "search_total_elements": res["total_elements"],
            "matches": matches,
        }

    def list_antibodies(
        self,
        page: int = 1,
        size: int = 100,
        updated_from: str | None = None,
        updated_to: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Unfiltered global listing via GET /api/antibodies (1-based pages).

        This route has NO text-search capability upstream; use
        :meth:`search_antibodies` for that.
        """
        params: dict[str, Any] = {"page": page, "size": size}
        if updated_from:
            params["updated_from"] = updated_from
        if updated_to:
            params["updated_to"] = updated_to
        if status:
            params["status"] = status
        data = self._get("/antibodies", params)
        items = [self._norm(r) for r in data.get("items", [])]
        return {
            "page": page,
            "total_elements": data.get("totalElements"),
            "retrieved": len(items),
            "items": items,
        }

    def datainfo(self) -> dict[str, Any]:
        """Registry size and last-update date via GET /api/datainfo."""
        return self._get("/datainfo")

    def vendors(self) -> list[dict[str, Any]]:
        """Vendor list via GET /api/vendors."""
        return self._get("/vendors")
