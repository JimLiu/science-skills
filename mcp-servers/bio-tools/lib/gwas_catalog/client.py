"""Throttled HTTP client for the NHGRI-EBI GWAS Catalog REST API v2 (JSON).

Politeness: one client instance enforces a minimum interval between requests
(default 0.5 s -> <= 2 requests/s) and retries 429/5xx responses and transport
errors at most once (MCP tools have a hard < 50 s wall-clock budget, so the
client favors failing loudly over long backoff).

The v2 API (https://www.ebi.ac.uk/gwas/rest/api/v2) serves flat JSON records
(snake_case fields, efo_traits/mapped_genes embedded inline) instead of the
HAL link-follow shapes of the v1 API. Quirk: unknown query parameters are
silently IGNORED (an unrecognized filter returns the entire 1.1M-row
association table), so callers must only send known-good filter names.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

BASE_URL = "https://www.ebi.ac.uk/gwas/rest/api/v2"
USER_AGENT = "gwas-catalog/0.1 (bio-tools fleet; python-requests)"


class GwasApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(GwasApiError):
    """HTTP 404 for a resource."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    raw_bodies: list = field(default_factory=list)


class GwasClient:
    """Throttled, bounded-retry GET-only client returning parsed JSON."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 30.0, max_retries: int = 2,
                 capture_raw: bool = False, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.capture_raw = capture_raw
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()

    def _throttle(self) -> None:




        pace(BASE_URL, self.min_interval_s)

    def get_json(self, path: str, params: dict | None = None):
        """GET ``path`` (relative to base_url) and parse JSON.

        Raises NotFound on HTTP 404 and GwasApiError after the single retry.
        """
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
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code in self.RETRY_STATUSES:
                last_err = GwasApiError(f"HTTP {resp.status_code} for {url}")
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, min(2 ** attempt, 4), cap=5.0)
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise GwasApiError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                raise GwasApiError(f"non-JSON body from {url}") from exc
            if self.capture_raw:
                self.stats.raw_bodies.append(resp.text)
            return payload
        raise GwasApiError(
            f"giving up on {url} after {self.max_retries} attempts: {last_err!r}")
