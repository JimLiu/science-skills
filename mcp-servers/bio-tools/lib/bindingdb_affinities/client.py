"""Throttled, retrying HTTP client for bindingdb.org/rest.

Politeness: hard minimum interval between requests (default 0.6 s). Budget:
30 s per-request timeout (hot targets return multi-MB JSON bodies — e.g.
EGFR at a 10 uM cutoff is ~8 MB / ~28k rows, which still transfers in ~1-2 s)
and at most one retry on 429/5xx/transport errors.

Quirks handled here:
* Response root keys are misspelled upstream (``getLindsByUniprotsResponse``)
  and vary per route — callers unwrap via :meth:`get_json_root`.
* Zero-hit queries are NOT errors: upstream returns well-formed JSON with an
  empty ``affinities`` list (verified live on both routes), which flows
  through to the callers' documented ``n_rows_total=0`` contract. An EMPTY
  200 body or an HTML page only occurs for broken routes/infra and raises
  :class:`BindingDbApiError` with the body prefix for context.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://bindingdb.org/rest"
USER_AGENT = "bindingdb-affinities/0.1 (bio-tools fleet; python-requests)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class BindingDbApiError(RuntimeError):
    """Unrecoverable BindingDB API error (including unparseable bodies)."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    per_url: list = field(default_factory=list)


class BindingDbClient:
    """Paced GET client returning the unwrapped JSON response root."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 min_interval_s: float = 0.6, timeout_s: float = 30.0,
                 max_retries: int = 1,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get_json_root(self, path: str, params: dict) -> dict:
        """GET a REST route; return the value under the single root key.

        Every BindingDB JSON response wraps its payload in one
        route-specific (and often misspelled) root key — this unwraps it so
        callers never depend on the exact spelling.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        params = {**params, "response": "application/json"}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params,
                                        timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2.0)
                continue
            self._last_request_t = time.monotonic()
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            self.stats.per_url.append({"url": url, "status": resp.status_code,
                                       "bytes": len(resp.content)})
            if resp.status_code in RETRY_STATUS:
                last_err = BindingDbApiError(f"HTTP {resp.status_code}")
                delay = retry_after_seconds(resp.headers.get("Retry-After", ""),
                                            2.0, cap=10.0)
                if attempt < self.max_retries:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise BindingDbApiError(
                    f"HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.text.strip()
            if not body:


                raise BindingDbApiError(
                    f"empty response body from {path} — upstream route "
                    f"failure (zero-hit queries return JSON with an empty "
                    f"affinities list, never an empty body)")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise BindingDbApiError(
                    f"non-JSON response from {path}: {body[:200]!r}") from exc
            if not isinstance(payload, dict) or len(payload) != 1:
                raise BindingDbApiError(
                    f"unexpected response shape from {path}: "
                    f"{str(payload)[:200]}")
            return next(iter(payload.values()))
        raise BindingDbApiError(
            f"giving up after {self.max_retries + 1} attempts: {last_err!r}")
