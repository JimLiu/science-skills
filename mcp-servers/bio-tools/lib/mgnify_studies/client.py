"""HTTP client for the MGnify (EBI Metagenomics) JSON:API v1.

Politeness: >= ``min_interval`` seconds between outbound requests (default 0.5 s,
i.e. <= 2 requests/s as requested for shared EBI hosts), retries with exponential
backoff on 429/5xx and connection errors, and instrumented request/byte counters
for benchmarking.
"""

from __future__ import annotations

import time
from typing import Any

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/metagenomics/api/v1"
USER_AGENT = "mgnify-studies/0.1 (bio-tools)"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class MGnifyError(RuntimeError):
    """Generic MGnify API error (after retries are exhausted) or pagination mismatch."""


class MGnifyNotFound(MGnifyError):
    """The requested resource does not exist (HTTP 404)."""


class MGnifyClient:
    """Polite, instrumented client for the MGnify JSON:API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval: float = 0.5,
        max_retries: int = 4,
        timeout: float = 60.0,
        page_size: int = 250,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.page_size = page_size
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})

        self.n_requests = 0
        self.bytes_downloaded = 0
        self.raw_log: list[str] | None = None
        self._last_request_at = 0.0




    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a JSON document, with throttling and retries.

        ``path_or_url`` may be a path relative to the API base (e.g. ``"studies/MGYS00000410"``)
        or an absolute URL (e.g. a JSON:API ``links.next`` URL).
        """
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._last_request_at = time.monotonic()
                self.n_requests += 1
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2.0**attempt)
                continue
            self._last_request_at = time.monotonic()
            self.n_requests += 1
            self.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                raise MGnifyNotFound(f"404 Not Found: {resp.url}")
            if resp.status_code in RETRYABLE_STATUS:
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, 2.0**attempt)
                last_err = MGnifyError(f"HTTP {resp.status_code} from {resp.url}")
                if attempt < self.max_retries:
                    time.sleep(delay)
                continue
            resp.raise_for_status()
            if self.raw_log is not None:
                self.raw_log.append(resp.text)
            return resp.json()
        raise MGnifyError(
            f"giving up on {url} after {self.max_retries + 1} attempts: {last_err}"
        )




    def get_all(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Retrieve every record of a paginated JSON:API collection.

        Follows ``links.next`` until exhausted and verifies that the number of
        retrieved (and de-duplicated) records equals ``meta.pagination.count``.

        Returns ``{"records": [...], "count": int, "pages_fetched": int}``.
        Raises :class:`MGnifyError` on a count mismatch (truncation or duplication).
        """
        params = dict(params or {})
        params.setdefault("page_size", self.page_size)
        records: list[dict[str, Any]] = []
        page = self.get_json(path, params=params)
        count = page["meta"]["pagination"]["count"]
        pages_fetched = 1
        records.extend(page.get("data", []))
        next_url = (page.get("links") or {}).get("next")
        while next_url:
            page = self.get_json(next_url)
            pages_fetched += 1
            records.extend(page.get("data", []))
            next_url = (page.get("links") or {}).get("next")
        unique_ids = {r["id"] for r in records}
        if len(records) != count or len(unique_ids) != count:
            raise MGnifyError(
                f"pagination mismatch on {path}: meta.pagination.count={count}, "
                f"retrieved={len(records)}, unique={len(unique_ids)}"
            )
        return {"records": records, "count": count, "pages_fetched": pages_fetched}
