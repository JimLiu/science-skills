"""HTTP client for the RCSB PDB search and data REST APIs.

Politeness: <= 2 requests/s per RCSB host (independent pacing for
search.rcsb.org and data.rcsb.org), identifying User-Agent, at most ONE retry
on 429/5xx/transport errors (tool budget: every call must finish well inside
the 60 s MCP transport limit — timeout 20 s, short backoff).
"""
from __future__ import annotations

import time
import urllib.parse

import httpx

from mcp_servers_common.ratelimit import retry_after_seconds

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
DATA_BASE_URL = "https://data.rcsb.org/rest/v1/core"
USER_AGENT = "pdb-structures/0.1 (bio-tools fleet; python-httpx)"
MIN_INTERVAL_S = 0.5
RETRY_STATUS = {429, 500, 502, 503, 504}


class PDBError(RuntimeError):
    """Raised when an RCSB API returns an unrecoverable error."""


class NotFoundError(PDBError):
    """Raised when a data-API resource does not exist (HTTP 404)."""


class PDBClient:
    """Thin HTTP wrapper with per-host throttling, bounded retries, accounting."""

    def __init__(
        self,
        search_url: str = SEARCH_URL,
        data_base_url: str = DATA_BASE_URL,
        timeout: float = 20.0,
        max_retries: int = 1,
        min_interval_s: float = MIN_INTERVAL_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.search_url = search_url
        self.data_base_url = data_base_url.rstrip("/")
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self._last_request_t: dict[str, float] = {}

        self.request_count = 0
        self.bytes_downloaded = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )


    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PDBClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


    def _throttle(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_request_t.get(host, 0.0)
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _request(self, method: str, url: str, json_body: dict | None = None) -> httpx.Response | None:
        """One throttled request with <= max_retries retries; None on 204."""
        host = urllib.parse.urlsplit(url).netloc
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            try:
                resp = self._client.request(method, url, json=json_body)
                self._last_request_t[host] = time.monotonic()
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
            except httpx.TransportError as exc:
                self._last_request_t[host] = time.monotonic()
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2.0)
                    continue
                raise PDBError(f"transport error for {url}: {exc}") from exc

            if resp.status_code == 200:
                return resp
            if resp.status_code == 204:
                return None
            if resp.status_code == 404:
                raise NotFoundError(f"not found: {url}")
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = retry_after_seconds(retry_after, 2.0, cap=10.0)
                time.sleep(wait)
                last_err = PDBError(f"HTTP {resp.status_code} for {url}")
                continue
            raise PDBError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
        raise PDBError(f"retries exhausted for {url}: {last_err}")


    def get_data(self, *segments: str) -> dict:
        """GET a data-API core resource, e.g. ``get_data("entry", "1TUP")``.

        Each segment is percent-encoded — model-controlled identifiers cannot
        reshape the request path. Raises NotFoundError on 404.
        """
        path = "/".join(urllib.parse.quote(str(s), safe="") for s in segments)
        url = f"{self.data_base_url}/{path}"
        resp = self._request("GET", url)
        if resp is None:
            raise PDBError(f"empty response for {url}")
        return resp.json()

    def post_search(self, payload: dict) -> dict | None:
        """POST a search-API JSON query. Returns None when the API answers 204 (zero hits)."""
        resp = self._request("POST", self.search_url, json_body=payload)
        return None if resp is None else resp.json()
