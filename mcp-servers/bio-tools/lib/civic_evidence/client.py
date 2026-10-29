"""GraphQL POST client for the CIViC API (https://civicdb.org/api/graphql).

The endpoint is POST-only: a GET request returns HTTP 404, so every operation
(including introspection as used by this tool's build) goes through a JSON POST
body of the form ``{"query": ..., "variables": ...}``.

Politeness: a single client instance enforces a minimum interval between
requests (default 0.5 s -> <= 2 requests/s) and retries 429/5xx responses and
transport errors with exponential backoff. GraphQL-level errors (the HTTP 200
``errors`` array) are surfaced as :class:`GraphQLError` and are NOT retried —
they are deterministic (bad field, bad enum value), so retrying would only
burn requests.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

BASE_URL = "https://civicdb.org/api/graphql"
USER_AGENT = "civic-evidence/0.1 (bio-tools benchmark; python-requests)"


class CivicApiError(RuntimeError):
    """Unrecoverable transport/HTTP error."""


class GraphQLError(CivicApiError):
    """The server answered 200 but with a GraphQL ``errors`` array."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(json.dumps(errors)[:1000])


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    raw_bodies: list = field(default_factory=list)


class CivicClient:
    """Throttled, retrying GraphQL-POST client returning the ``data`` payload."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 60.0, max_retries: int = 5,
                 capture_raw: bool = False, session: requests.Session | None = None):
        self.base_url = base_url
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.capture_raw = capture_raw
        self.session = session or requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        self.stats = TransportStats()
        self._last_request_t = 0.0


    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)


    def execute(self, query: str, variables: dict | None = None) -> dict:
        """POST one GraphQL operation; return the parsed ``data`` object.

        Raises GraphQLError when the response carries an ``errors`` array and
        CivicApiError after exhausting retries on transport/HTTP failures.
        """
        body = {"query": query, "variables": variables or {}}
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.post(self.base_url, json=body, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                continue
            self._last_request_t = time.monotonic()
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code in self.RETRY_STATUSES:
                last_err = CivicApiError(f"HTTP {resp.status_code} from {self.base_url}")
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, min(2 ** attempt, 30))
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise CivicApiError(
                    f"HTTP {resp.status_code} from {self.base_url}: {resp.text[:300]}")
            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                continue
            if payload.get("errors"):
                raise GraphQLError(payload["errors"])
            if self.capture_raw:
                self.stats.raw_bodies.append(resp.text)
            return payload.get("data") or {}
        raise CivicApiError(
            f"giving up on {self.base_url} after {self.max_retries} attempts: {last_err!r}")
