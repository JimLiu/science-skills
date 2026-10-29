"""HTTP client for the STRING REST API.

Features:
  * version-pinned base URL (https://version-12-0.string-db.org/api) so results
    are reproducible against a stated STRING release,
  * polite caller_identity sent with every request,
  * client-side rate limiting (default >= 0.34 s between requests, ~3 req/s),
  * bounded retries with exponential backoff on 429/5xx and connection errors,
  * request accounting (count, bytes, per-request log) for provenance and
    benchmarking.
"""

from __future__ import annotations

import time
from typing import Any

import requests

DEFAULT_BASE_URL = "https://version-12-0.string-db.org/api"
FALLBACK_BASE_URL = "https://string-db.org/api"
DEFAULT_CALLER_IDENTITY = "bio-tools-string-network"
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


class StringApiError(RuntimeError):
    """Raised when the STRING API returns a non-recoverable error."""


class StringClient:
    """Thin requests-based client for the STRING REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        caller_identity: str = DEFAULT_CALLER_IDENTITY,
        min_interval_s: float = 0.34,
        max_attempts: int = 4,
        backoff_s: float = 1.0,
        timeout_s: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.caller_identity = caller_identity
        self.min_interval_s = min_interval_s
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": caller_identity})


        self.n_requests = 0
        self.bytes_downloaded = 0
        self.request_log: list[dict[str, Any]] = []

        self._last_request_t = 0.0
        self._version_cache: list[dict[str, Any]] | None = None



    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def reset_counters(self) -> None:
        self.n_requests = 0
        self.bytes_downloaded = 0
        self.request_log = []



    def call(self, output_format: str, endpoint: str, params: dict[str, Any]) -> str:
        """POST to /api/<output_format>/<endpoint> and return the response body text.

        Retries on 429/5xx and connection errors with exponential backoff.
        Every attempt that goes out on the wire is counted in ``n_requests``.
        """
        url = f"{self.base_url}/{output_format}/{endpoint}"
        payload = dict(params)
        payload.setdefault("caller_identity", self.caller_identity)

        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            t0 = time.monotonic()
            self._last_request_t = time.monotonic()
            try:
                resp = self.session.post(url, data=payload, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self.n_requests += 1
                last_error = f"{type(exc).__name__}: {exc}"
                self.request_log.append(
                    {
                        "endpoint": f"{output_format}/{endpoint}",
                        "attempt": attempt,
                        "status": None,
                        "bytes": 0,
                        "elapsed_s": round(time.monotonic() - t0, 3),
                        "error": last_error,
                    }
                )
                if attempt < self.max_attempts:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue

            elapsed = time.monotonic() - t0
            nbytes = len(resp.content)
            self.n_requests += 1
            self.bytes_downloaded += nbytes
            self.request_log.append(
                {
                    "endpoint": f"{output_format}/{endpoint}",
                    "attempt": attempt,
                    "status": resp.status_code,
                    "bytes": nbytes,
                    "elapsed_s": round(elapsed, 3),
                }
            )

            if resp.status_code == 200:
                return resp.text
            if resp.status_code in RETRY_STATUS_CODES and attempt < self.max_attempts:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            raise StringApiError(
                f"STRING API error for {output_format}/{endpoint}: "
                f"HTTP {resp.status_code}: {resp.text[:500]}"
            )

        raise StringApiError(
            f"STRING API request failed for {output_format}/{endpoint} "
            f"after {self.max_attempts} attempts (last error: {last_error})"
        )



    def get_version(self) -> dict[str, Any]:
        """Return {'string_version': ..., 'stable_address': ...} (cached per client)."""
        if self._version_cache is None:
            import json

            text = self.call("json", "version", {})
            self._version_cache = json.loads(text)
        return dict(self._version_cache[0])
