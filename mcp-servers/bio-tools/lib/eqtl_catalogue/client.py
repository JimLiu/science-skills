"""eqtl-catalogue: bounded retrieval from the eQTL Catalogue API v2.

The v2 API (https://www.ebi.ac.uk/eqtl/api/v2) serves bare JSON lists with
``start``/``size`` pagination and NO total count, so completeness is proven
by exhaustion: a page shorter than ``size`` ends the walk. Capped walks set
``truncated=True`` — silent truncation is impossible.

Quirks handled here:
  * an empty result is HTTP 400 with body ``{"message": "No results"}`` —
    normalized to an empty list;
  * unfiltered /associations queries are rejected upstream (400 "Query is
    not permitted...") — surfaced as a clear error;
  * ``size`` is capped at 1000 upstream (422 beyond that).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

BASE_URL = "https://www.ebi.ac.uk/eqtl/api/v2"
USER_AGENT = "eqtl-catalogue/0.1 (bio-tools fleet; python-requests)"
PAGE_SIZE = 1000
MAX_PAGES = 8


class EqtlApiError(RuntimeError):
    """Unrecoverable API or transport error."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0


class EqtlClient:
    """Throttled, bounded-retry GET-only client returning parsed JSON.

    ``get_json`` returns ``[]`` for the API's 400 "No results" idiom.
    """

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 30.0, max_retries: int = 2,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()

    def _throttle(self) -> None:




        pace(BASE_URL, self.min_interval_s)

    def get_json(self, path: str, params: dict | None = None):
        url = path if path.startswith("http") else self.base_url + path
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 4))
                continue
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code == 400:
                try:
                    msg = resp.json().get("message", "")
                except json.JSONDecodeError:
                    msg = resp.text[:200]
                if msg == "No results":
                    return []
                raise EqtlApiError(f"HTTP 400 for {url}: {msg}")
            if resp.status_code in self.RETRY_STATUSES:
                last_err = EqtlApiError(f"HTTP {resp.status_code} for {url}")
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, min(2 ** attempt, 4), cap=5.0)
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise EqtlApiError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise EqtlApiError(f"non-JSON body from {url}") from exc
        raise EqtlApiError(
            f"giving up on {url} after {self.max_retries} attempts: {last_err!r}")


class EqtlCatalogue:
    """High-level eQTL Catalogue v2 interface with exhaustion-proven walks."""

    def __init__(self, client: EqtlClient | None = None,
                 page_size: int = PAGE_SIZE):
        self.client = client or EqtlClient()
        self.page_size = page_size

    def _walk(self, path: str, params: dict[str, Any], max_records: int
              ) -> tuple[list[dict], bool]:
        """Walk start/size pages. Returns (rows, truncated).

        Completeness is proven by exhaustion (the API has no total count),
        so each request asks for one row beyond what the cap still needs
        (bounded by the upstream size limit): a batch shorter than requested
        proves the set is exhausted — including when it exactly fills the
        cap — and any surplus row proves truncation."""
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        rows: list[dict] = []
        start, pages = 0, 0
        while True:
            want = max_records - len(rows)
            size = min(self.page_size, want + 1)
            q = dict(params)
            q.update({"start": start, "size": size})
            batch = self.client.get_json(path, params=q)
            if not isinstance(batch, list):
                raise EqtlApiError(f"unexpected non-list payload from {path}")
            rows.extend(batch)
            if len(batch) < size:

                return rows[:max_records], len(rows) > max_records
            if len(rows) > max_records:
                return rows[:max_records], True
            if pages >= MAX_PAGES - 1:



                q = dict(params)
                q.update({"start": start + len(batch), "size": 1})
                peek = self.client.get_json(path, params=q)
                return rows[:max_records], bool(peek)
            start += size
            pages += 1

    def datasets(self, filters: dict[str, Any], max_records: int
                 ) -> dict[str, Any]:
        rows, truncated = self._walk("/datasets", filters, max_records)
        rows.sort(key=lambda d: d.get("dataset_id") or "")
        return {"returned": len(rows), "truncated": truncated,
                "datasets": rows}

    def associations(self, dataset_id: str, filters: dict[str, Any],
                     max_records: int) -> dict[str, Any]:



        did = str(dataset_id).strip()
        if not re.fullmatch(r"QTD\d+", did):
            raise EqtlApiError(
                f"invalid dataset_id {dataset_id!r} — expected the QTD "
                f"grammar (QTD followed by digits, e.g. 'QTD000584'); see "
                f"eqtl_list_datasets")
        rows, truncated = self._walk(
            f"/datasets/{did}/associations", filters, max_records)
        return {"dataset_id": dataset_id, "returned": len(rows),
                "truncated": truncated, "associations": rows}
