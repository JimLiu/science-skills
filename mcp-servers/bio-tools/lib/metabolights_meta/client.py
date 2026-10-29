"""Rate-limited, retrying HTTP client for the MetaboLights web service.

Design notes
------------
* Politeness: <= 2 requests/second against www.ebi.ac.uk (enforced with a minimum
  inter-request interval; default 0.5 s).
* Retries: connection errors, read timeouts, HTTP 5xx/429, and JSON decode failures are
  retried with exponential backoff (the MetaboLights WS occasionally returns transient
  502/504 from the EBI front-end). HTTP 401/403/404 are NOT retried — they indicate a
  private, deprecated, or non-existent study.
* Instrumentation: every outbound request increments ``n_requests`` and adds the size
  of the response body to ``bytes_downloaded`` so benchmarks can read exact counts.
* Testability: a ``transport`` callable ``(url, params, timeout) -> (status_code, bytes)``
  can be injected to run fully offline in unit tests.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional, Tuple

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/metabolights/ws"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MIN_INTERVAL = 0.5
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 1.0

Transport = Callable[[str, Optional[dict], float], Tuple[int, bytes]]


class MetaboLightsError(Exception):
    """Base error for the metabolights_meta package."""


class MetaboLightsHTTPError(MetaboLightsError):
    """Raised when the web service returns a non-retryable error or retries are exhausted."""

    def __init__(self, message: str, status_code: Optional[int] = None, url: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class MetaboLightsNotFoundError(MetaboLightsHTTPError):
    """Raised for HTTP 401/403/404 — the study is private, deprecated, or does not exist."""


def _requests_transport(url: str, params: Optional[dict], timeout: float) -> Tuple[int, bytes]:
    """Default transport using the ``requests`` library (imported lazily)."""
    import requests

    resp = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": "metabolights-meta/0.1.0 (bio-tools; python-requests)",
        },
    )
    return resp.status_code, resp.content


class MetaboLightsClient:
    """HTTP client for the MetaboLights web service (www.ebi.ac.uk/metabolights/ws)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = DEFAULT_MIN_INTERVAL,
        timeout_s: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self._transport: Transport = transport or _requests_transport
        self._sleep = sleep
        self._last_request_t: float = 0.0

        self.n_requests: int = 0
        self.bytes_downloaded: int = 0


    def reset_counters(self) -> None:
        self.n_requests = 0
        self.bytes_downloaded = 0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            self._sleep(self.min_interval_s - elapsed)

    def get_json(self, path: str, params: Optional[dict] = None) -> Any:
        """GET ``base_url + path`` and return the parsed JSON body.

        Retries transient failures (connection errors, 5xx/429, malformed JSON) up to
        ``max_retries`` times with exponential backoff. Raises
        :class:`MetaboLightsNotFoundError` for 401/403/404 and
        :class:`MetaboLightsHTTPError` otherwise.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_err: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_t = time.monotonic()
            try:
                status, body = self._transport(url, params, self.timeout_s)
            except Exception as exc:
                last_err = f"transport error: {exc!r}"
                self.n_requests += 1
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            self.n_requests += 1
            self.bytes_downloaded += len(body)

            if status in (401, 403, 404):
                raise MetaboLightsNotFoundError(
                    f"HTTP {status} for {url} (study private, deprecated, or not found)",
                    status_code=status,
                    url=url,
                )
            if status >= 500 or status == 429:
                last_err = f"HTTP {status}"
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            if status != 200:
                raise MetaboLightsHTTPError(
                    f"HTTP {status} for {url}", status_code=status, url=url
                )
            try:
                return json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                last_err = f"JSON decode error: {exc}"
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
        raise MetaboLightsHTTPError(
            f"giving up on {url} after {self.max_retries + 1} attempts ({last_err})", url=url
        )

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            self._sleep(self.backoff_base_s * (2 ** attempt))
