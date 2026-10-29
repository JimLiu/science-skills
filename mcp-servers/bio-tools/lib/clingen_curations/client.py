"""Throttled, retrying HTTP client for the three ClinGen public hosts.

Hosts (do not conflate — they are distinct services):
  * https://search.clinicalgenome.org        — gene validity + dosage tables
  * https://actionability.clinicalgenome.org — clinical actionability summaries
  * https://erepo.genome.network/evrepo/api  — Evidence Repository (VCEP variant
    classifications)

Politeness: a single client instance enforces a minimum interval between
requests (default 0.5 s -> <= 2 req/s across all three hosts) and retries
429/5xx and transport errors with exponential backoff.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

SEARCH_BASE = "https://search.clinicalgenome.org"
ACTIONABILITY_BASE = "https://actionability.clinicalgenome.org"
EREPO_BASE = "https://erepo.genome.network/evrepo/api"
USER_AGENT = "clingen-curations/0.1 (bio-tools benchmark; python-requests)"


class ClinGenApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(ClinGenApiError):
    """HTTP 404 for a resource."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    raw_bodies: list = field(default_factory=list)


class ClinGenClient:
    """Throttled, retrying GET-only client."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, min_interval_s: float = 0.5, timeout_s: float = 180.0,
                 max_retries: int = 5, capture_raw: bool = False,
                 session: requests.Session | None = None):
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.capture_raw = capture_raw
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
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
                last_err = ClinGenApiError(f"HTTP {resp.status_code} for {url}")
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, min(2 ** attempt, 30))
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise ClinGenApiError(
                    f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
            if self.capture_raw:
                self.stats.raw_bodies.append(resp.text)
            return resp
        raise ClinGenApiError(
            f"giving up on {url} after {self.max_retries} attempts: {last_err!r}")

    def get_json(self, url: str, params: dict | None = None):
        resp = self._get(url, params=params)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise ClinGenApiError(f"non-JSON response from {url}: {exc}") from exc

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self._get(url, params=params).text
