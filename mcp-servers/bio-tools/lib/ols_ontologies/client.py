"""Throttled, retrying HTTP client for the EBI OLS4 REST API.

Politeness: EBI shared host -> at most 2 requests/second (>= 0.5 s between
requests). Retries on 429/5xx and transport errors with exponential backoff.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/ols4/api"
USER_AGENT = "bio-tools/ols-ontologies 0.1.0 (https://github.com/anthropic-experimental/bio-tools)"


class OLSClient:
    """Minimal GET-only client with request spacing, retries and counters."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = 0.5,
        max_retries: int = 3,
        timeout_s: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        self._last_request_at = 0.0

        self.request_count = 0
        self.bytes_downloaded = 0


    def __enter__(self) -> "OLSClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()


    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def get_json(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET base_url+path, return parsed JSON, or None on HTTP 404.

        Raises RuntimeError after exhausting retries on other failures.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_err: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url, params=params)
                self._last_request_at = time.monotonic()
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
            except httpx.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_err = f"transport error: {exc!r}"
                if attempt < self.max_retries:
                    time.sleep(min(2.0 * (2 ** attempt), 10.0))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                delay = retry_after_seconds(retry_after, 2.0 * (2 ** attempt))
                if attempt < self.max_retries:
                    time.sleep(min(delay, 30.0))
                continue
            raise RuntimeError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError(f"GET {url} failed after {self.max_retries + 1} attempts ({last_err})")
