"""HTTP layer: a thin requests wrapper with rate limiting, retries and instrumentation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/QuickGO/services"
USER_AGENT = "quickgo-annotations/0.1.0 (bio-tools; python-requests)"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class TransportStats:
    """Counts every outbound HTTP request and the bytes received."""
    http_requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    by_endpoint: dict = field(default_factory=dict)

    def record(self, endpoint: str, nbytes: int) -> None:
        self.http_requests += 1
        self.bytes_downloaded += nbytes
        self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + 1


class QuickGOClient:
    """Rate-limited, retrying HTTP client for the QuickGO services.

    Parameters
    ----------
    min_interval_s : float
        Minimum spacing between outbound requests (default 0.5 s -> <= 2 req/s,
        per EBI politeness guidance).
    max_retries : int
        Attempts per request on retryable failures (HTTP 429/5xx, connection
        errors), with exponential backoff.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, min_interval_s: float = 0.5,
                 max_retries: int = 4, timeout_s: float = 60.0,
                 session: requests.Session | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.stats = TransportStats()
        self._last_request_t = 0.0


    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict, accept: str) -> requests.Response:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                self._last_request_t = time.monotonic()
                resp = self.session.get(url, params=params,
                                        headers={"Accept": accept},
                                        timeout=self.timeout_s)
                self.stats.record(path.split("?")[0], len(resp.content))
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.stats.retries += 1
                if attempt < self.max_retries - 1:
                    time.sleep(min(2.0 ** attempt, 8.0))
                continue
            if resp.status_code in RETRYABLE_STATUS:
                self.stats.retries += 1
                if attempt < self.max_retries - 1:
                    time.sleep(min(2.0 ** attempt, 8.0))
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
                continue
            if resp.status_code != 200:
                raise requests.HTTPError(
                    f"QuickGO returned HTTP {resp.status_code} for {url}: "
                    f"{resp.text[:500]}")
            return resp
        raise RuntimeError(f"QuickGO request failed after {self.max_retries} attempts: "
                           f"{url} ({last_exc})")


    def get_json(self, path: str, params: dict | None = None) -> dict:
        return self._get(path, params or {}, "application/json").json()

    def get_tsv(self, path: str, params: dict | None = None) -> str:
        return self._get(path, params or {}, "text/tsv").text
