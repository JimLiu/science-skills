"""Throttled JSON GET client shared by the UniBind REST and UCSC hubApi hosts.

Politeness: one client instance per host enforces a minimum interval between
requests (default 0.5 s -> <= 2 req/s). Budget discipline (MCP transport
allows < 50 s per tool call): request timeout 20 s and AT MOST ONE retry on
429/5xx/transport errors — a second failure raises instead of burning the
tool budget on backoff.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

from mcp_servers_common.ratelimit import retry_after_seconds

USER_AGENT = "unibind-tfbs/0.1 (bio-tools fleet; python-requests)"


class UniBindApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(UniBindApiError):
    """HTTP 404 for a resource (unknown dataset tf_id)."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0


class PacedJsonClient:
    """Paced, lightly-retrying GET-only client returning parsed JSON.

    One instance per upstream host so the politeness interval is enforced
    per-host (instances for unibind.uio.no and api.genome.ucsc.edu pace
    independently).
    """

    RETRY_STATUSES = {429, 500, 502, 503, 504}


    OK_STATUSES = {200, 206}

    def __init__(self, base_url: str, min_interval_s: float = 0.5,
                 timeout_s: float = 20.0, retry_sleep_s: float = 2.0,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.retry_sleep_s = retry_sleep_s
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get_json(self, path: str, params: dict | None = None):
        """GET ``path`` (relative to base_url, or an absolute DRF ``next`` URL).

        Raises NotFound on HTTP 404 and UniBindApiError after the single
        permitted retry fails.
        """
        url = path if path.startswith("http") else self.base_url + path
        last_err: Exception | None = None
        for attempt in range(2):
            self._throttle()
            try:
                resp = self.session.get(url, params=params,
                                        timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt == 0:
                    time.sleep(self.retry_sleep_s)
                continue
            self._last_request_t = time.monotonic()
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code in self.RETRY_STATUSES:
                last_err = UniBindApiError(f"HTTP {resp.status_code} for {url}")
                if attempt == 0:
                    retry_after = resp.headers.get("Retry-After", "")
                    time.sleep(retry_after_seconds(
                        retry_after, self.retry_sleep_s, cap=10.0))
                continue
            if resp.status_code not in self.OK_STATUSES:
                raise UniBindApiError(
                    f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise UniBindApiError(
                    f"non-JSON response from {url}: {exc}") from exc
        raise UniBindApiError(f"giving up on {url} after 2 attempts: {last_err!r}")
