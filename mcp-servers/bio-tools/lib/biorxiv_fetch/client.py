"""Low-level HTTP client for the bioRxiv/medRxiv API (https://api.biorxiv.org).

Politeness: a single client instance enforces a minimum interval between
requests (default 0.5 s -> <= 2 requests/s) and retries 429/5xx responses and
transport errors with exponential backoff.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

BASE_URL = "https://api.biorxiv.org"
USER_AGENT = "biorxiv-fetch/0.1 (bio-tools benchmark; python-requests)"


class BiorxivApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(BiorxivApiError):
    """The API reported 'no posts found' for a direct DOI lookup."""


class IncompleteRetrieval(BiorxivApiError):
    """A cursor walk ended with fewer records than the API-reported total."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0


class BiorxivClient:
    """Throttled, retrying GET-only client returning parsed JSON."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 60.0, max_retries: int = 5,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get_json(self, path: str):
        """GET ``path`` (relative to base_url) and parse the JSON body."""
        url = self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout_s)
                self._last_request_t = time.monotonic()
                self.stats.requests += 1
                self.stats.bytes_downloaded += len(resp.content)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code in self.RETRY_STATUSES:
                last_exc = BiorxivApiError(f"HTTP {resp.status_code} for {url}")
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 404:
                raise NotFound(f"HTTP 404 for {url}")
            if resp.status_code != 200:
                raise BiorxivApiError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise BiorxivApiError(f"non-JSON body from {url}: {resp.text[:200]}") from exc
        raise BiorxivApiError(f"retries exhausted for {url}: {last_exc}")
