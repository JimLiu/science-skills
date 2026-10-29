"""HTTP client for the PRIDE Archive REST API v2.

Politeness: a hard minimum interval between requests (default 0.55 s -> < 2 req/s,
per EBI guidance) and bounded retries with exponential backoff on transient errors.

The client instruments every outbound request so benchmarks can report exact
request counts and downloaded byte totals.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/pride/ws/archive/v2"
USER_AGENT = "pride-projects/0.1 (bio-tools; python-httpx)"


RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class RequestStats:
    """Counters accumulated across all requests made by one client."""

    requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    per_url: list = field(default_factory=list)

    def record(self, url: str, status: int, nbytes: int) -> None:
        self.requests += 1
        self.bytes_downloaded += nbytes
        self.per_url.append({"url": url, "status": status, "bytes": nbytes})


class PrideClient:
    """Thin, polite wrapper around the PRIDE Archive v2 REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        min_interval: float = 0.55,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.stats = RequestStats()
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=transport,
        )


    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PrideClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET ``base_url + path`` with throttling and bounded retries.

        Raises ``httpx.HTTPStatusError`` for non-retryable error statuses and for
        retryable ones that persist past ``max_retries``.
        """
        url = self.base_url + path
        attempt = 0
        while True:
            self._throttle()
            try:
                self._last_request_at = time.monotonic()
                resp = self._client.get(url, params=params)
                self.stats.record(str(resp.request.url), resp.status_code, len(resp.content))
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                self.stats.retries += 1
                time.sleep(self.backoff_base * (2 ** (attempt - 1)))
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                attempt += 1
                self.stats.retries += 1
                time.sleep(self.backoff_base * (2 ** (attempt - 1)))
                continue

            resp.raise_for_status()
            return resp
