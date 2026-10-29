"""Throttled, retrying HTTP client for the ChEBI public backend REST API.

Base: https://www.ebi.ac.uk/chebi/backend/api/public (the API behind the
2024+ ChEBI website — keyless JSON; the legacy SOAP web services are being
retired).

Politeness: hard minimum interval between requests (default 0.55 s ->
< 2 req/s per EBI guidance). Budget: 15 s per-request timeout, at most one
retry on 429/5xx/transport errors.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/chebi/backend/api/public"
USER_AGENT = "chebi-ontology/0.1 (bio-tools fleet; python-requests)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class ChebiApiError(RuntimeError):
    """Unrecoverable ChEBI API error."""


class NotFound(ChebiApiError):
    """The API reported no entity for the requested ID (HTTP 404)."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    per_url: list = field(default_factory=list)


class ChebiClient:
    """Paced GET client returning parsed JSON payloads."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 min_interval_s: float = 0.55, timeout_s: float = 15.0,
                 max_retries: int = 1,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept": "application/json"})
        self.stats = TransportStats()

    def _throttle(self) -> None:




        pace(DEFAULT_BASE_URL, self.min_interval_s)

    def get_json(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params,
                                        timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2.0)
                continue
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            self.stats.per_url.append({"url": url, "status": resp.status_code,
                                       "bytes": len(resp.content)})
            if resp.status_code in RETRY_STATUS:
                last_err = ChebiApiError(f"HTTP {resp.status_code}")
                delay = retry_after_seconds(resp.headers.get("Retry-After", ""),
                                            2.0, cap=10.0)
                if attempt < self.max_retries:
                    time.sleep(delay)
                continue
            if resp.status_code == 404:

                try:
                    detail = resp.json().get("detail", "")
                except json.JSONDecodeError:
                    detail = ""
                raise NotFound(detail or f"404 for {path}")
            if resp.status_code != 200:
                raise ChebiApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise ChebiApiError(f"non-JSON response from {url}: {exc}") from exc
        raise ChebiApiError(
            f"giving up after {self.max_retries + 1} attempts: {last_err!r}")
