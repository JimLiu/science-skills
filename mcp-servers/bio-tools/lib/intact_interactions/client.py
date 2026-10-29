"""HTTP client for the IntAct web service (www.ebi.ac.uk/intact/ws).

Centralizes politeness (rate limiting), retries, and instrumentation
(request count, wire bytes) so every caller goes through one code path.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/intact/ws"
USER_AGENT = "intact-interactions/0.1 (bio-tools wave 3; httpx)"



RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class IntActError(RuntimeError):
    """Raised when the IntAct web service returns an unrecoverable error."""


class IntActClient:
    """Thin instrumented wrapper around httpx for the IntAct web service.

    Parameters
    ----------
    base_url:
        Root of the IntAct web service (default: the public EBI endpoint).
    min_interval_s:
        Minimum spacing between outbound requests (politeness; EBI hosts
        are limited to <= 2 requests/second in this project, so 0.5 s).
    max_retries:
        Number of retries for transient failures (5xx, 429, network errors,
        malformed JSON bodies) with exponential backoff.
    timeout_s:
        Per-request timeout.
    transport:
        Optional httpx transport (used by offline tests with MockTransport).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = 0.5,
        max_retries: int = 4,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.http_requests = 0
        self.bytes_downloaded = 0
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=transport,
        )



    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "IntActClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()



    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> Any:
        """Issue one request and return the parsed JSON body.

        Retries transient failures (network errors, 5xx, 429, bodies that do
        not parse as JSON) with exponential backoff. Raises IntActError when
        retries are exhausted or on a permanent (non-retryable) HTTP error.

        With ``allow_empty=True`` an HTTP-200 response with an empty body
        returns ``None`` instead of being treated as a retryable JSON error
        (the graph-ws detail routes answer unknown accessions this way).
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(min(2.0 ** attempt, 30.0))
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self._client.request(method, url, params=params)
            except httpx.HTTPError as exc:
                self.http_requests += 1
                last_error = f"transport error: {exc!r}"
                continue
            self.http_requests += 1
            self.bytes_downloaded += response.num_bytes_downloaded
            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise IntActError(
                    f"{method} {url} -> HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            if allow_empty and not response.content.strip():
                return None
            try:
                return response.json()
            except json.JSONDecodeError:
                last_error = "response body was not valid JSON"
                continue
        raise IntActError(
            f"{method} {url} failed after {self.max_retries + 1} attempts "
            f"(last error: {last_error})"
        )

    def get_json(self, path: str, params: dict[str, Any] | None = None,
                 allow_empty: bool = False) -> Any:
        return self.request_json("GET", path, params, allow_empty=allow_empty)

    def post_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request_json("POST", path, params)
