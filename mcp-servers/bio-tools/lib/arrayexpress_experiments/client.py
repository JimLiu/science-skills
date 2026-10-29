"""Throttled, retrying, instrumented HTTP client for the BioStudies API."""
from __future__ import annotations

import json
import time
from typing import Any

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/biostudies/api/v1"
USER_AGENT = "arrayexpress-experiments/0.1 (bio-tools; python-requests)"


class BioStudiesError(RuntimeError):
    """Raised when the BioStudies API returns an unrecoverable error."""


class BioStudiesClient:
    """Minimal client for www.ebi.ac.uk/biostudies/api/v1.

    * polite: enforces a minimum interval between requests (default 0.51 s, i.e. <= 2 req/s)
    * robust: retries 429 / 5xx / connection errors with exponential backoff
    * instrumented: counts HTTP requests and downloaded bytes (for benchmarking)
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = 0.51,
        max_retries: int = 4,
        timeout_s: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.session.headers.setdefault("Accept", "application/json")
        self._last_request_t = 0.0

        self.request_count = 0
        self.bytes_downloaded = 0

    def reset_counters(self) -> None:
        self.request_count = 0
        self.bytes_downloaded = 0

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)
        self._last_request_t = time.monotonic()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``base_url + path`` and return parsed JSON, with throttling and retries."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After")
                    delay = retry_after_seconds(retry_after, 2.0 ** attempt)
                    last_exc = BioStudiesError(f"HTTP {resp.status_code} for {url}")
                    if attempt < self.max_retries:
                        time.sleep(delay)
                    continue
                if resp.status_code == 404:
                    raise BioStudiesError(f"Not found (HTTP 404): {url} params={params}")
                resp.raise_for_status()
                try:
                    return resp.json()
                except json.JSONDecodeError as exc:
                    raise BioStudiesError(f"Non-JSON response from {url}") from exc
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(2.0 ** attempt)
                continue
        raise BioStudiesError(f"Giving up on {url} after {self.max_retries + 1} attempts: {last_exc}")

    def get_text(self, url: str) -> str:
        """GET an absolute URL (e.g. the public files endpoint) and return the body
        as text, following redirects. Same throttling/retry/instrumentation as
        :meth:`get_json`."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout_s, allow_redirects=True)
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After")
                    delay = retry_after_seconds(retry_after, 2.0 ** attempt)
                    last_exc = BioStudiesError(f"HTTP {resp.status_code} for {url}")
                    if attempt < self.max_retries:
                        time.sleep(delay)
                    continue
                if resp.status_code == 404:
                    raise BioStudiesError(f"Not found (HTTP 404): {url}")
                resp.raise_for_status()
                return resp.text
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(2.0 ** attempt)
                continue
        raise BioStudiesError(f"Giving up on {url} after {self.max_retries + 1} attempts: {last_exc}")
