"""Throttled, retrying GraphQL POST client for the gnomAD API.

Politeness: requests are paced at gnomAD's published limit of 10 queries per
minute per IP (min_interval_s=6.0), enforced process-wide via
mcp_servers_common.ratelimit (the earlier 0.5 s default ran 12x over the
published cap). Retries 429/5xx and transport errors with
exponential backoff. The gnomAD API is GraphQL-only: every call is a POST of
{"query": ..., "variables": ...} to https://gnomad.broadinstitute.org/api.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

BASE_URL = "https://gnomad.broadinstitute.org/api"
USER_AGENT = "gnomad-variants/0.1 (bio-tools fleet; python-requests)"


class GnomadApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(GnomadApiError):
    """The API reported the requested entity does not exist."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    raw_bodies: list = field(default_factory=list)


class GnomadClient:
    """GraphQL POST client returning the parsed ``data`` payload."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    NOT_FOUND_MESSAGES = {
        "variant not found",
        "gene not found",
        "transcript not found",
        "structural variant not found",
        "copy number variant not found",
    }

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 6.0,
                 timeout_s: float = 60.0, max_retries: int = 3,
                 capture_raw: bool = False, session: requests.Session | None = None):
        self.base_url = base_url
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.capture_raw = capture_raw
        self.session = session or requests.Session()
        self.session.headers.update({"Content-Type": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0

    def _throttle(self) -> None:

        pace(self.base_url, self.min_interval_s)

    def query(self, document: str, variables: dict | None = None):
        """POST a GraphQL document; return the ``data`` dict.

        Raises NotFound when the only GraphQL errors are entity-not-found
        messages, GnomadApiError on schema/complexity errors or after
        exhausting retries on transport/5xx failures.
        """
        body = {"query": document, "variables": variables or {}}
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
                last_err = GnomadApiError(f"HTTP {resp.status_code}")
                retry_after = resp.headers.get("Retry-After", "")
                if resp.status_code == 429:




                    fallback = min(60.0 * (attempt + 1), 120.0)
                else:
                    fallback = min(2 ** attempt, 30)



                delay = retry_after_seconds(retry_after, fallback)
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise GnomadApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                continue
            if self.capture_raw:
                self.stats.raw_bodies.append(resp.text)
            errors = payload.get("errors")
            if errors:
                messages = [str(e.get("message", "")) for e in errors]
                if all(m.strip().lower() in self.NOT_FOUND_MESSAGES for m in messages):
                    raise NotFound("; ".join(messages))
                raise GnomadApiError(f"GraphQL errors: {messages}")
            return payload.get("data") or {}
        raise GnomadApiError(f"giving up after {self.max_retries} attempts: {last_err!r}")
