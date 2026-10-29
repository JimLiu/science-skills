"""Low-level HTTP client for the GTEx Portal API v2 (JSON).

Politeness: a single client instance enforces a minimum interval between
requests (default 0.5 s -> <= 2 requests/s) and retries 429/5xx responses and
transport errors with exponential backoff.

List-valued query parameters (e.g. several ``gencodeId`` values) are encoded
as repeated keys, which is what the GTEx v2 API expects.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

BASE_URL = "https://gtexportal.org/api/v2"
USER_AGENT = "gtex-expression/0.1 (bio-tools benchmark; python-requests)"


class GtexApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(GtexApiError):
    """HTTP 404 for a resource."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    raw_bodies: list = field(default_factory=list)


class GtexClient:
    """Throttled, retrying GET-only client returning parsed JSON."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 90.0, max_retries: int = 5,
                 capture_raw: bool = False, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.capture_raw = capture_raw
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0


    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)


    def get_json(self, path: str, params: dict | None = None):
        """GET ``path`` (relative to base_url) and parse JSON.

        ``params`` values may be lists (encoded as repeated keys).
        Raises NotFound on HTTP 404 and GtexApiError after exhausting retries.
        """
        url = path if path.startswith("http") else self.base_url + path
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                continue
            self._last_request_t = time.monotonic()
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code in self.RETRY_STATUSES:
                last_err = GtexApiError(f"HTTP {resp.status_code} for {url}")
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, min(2 ** attempt, 30))
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise GtexApiError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                continue
            if self.capture_raw:
                self.stats.raw_bodies.append(resp.text)
            return payload
        raise GtexApiError(f"giving up on {url} after {self.max_retries} attempts: {last_err!r}")
