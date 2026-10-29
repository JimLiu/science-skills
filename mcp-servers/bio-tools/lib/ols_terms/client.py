"""HTTP client for the EBI OLS4 REST API with retries, rate limiting and instrumentation."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from mcp_servers_common.ratelimit import CappedRetry
from requests.adapters import HTTPAdapter

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/ols4/api"
DEFAULT_TIMEOUT = 60.0

DEFAULT_MIN_INTERVAL_S = 0.5
DEFAULT_MAX_RETRIES = 5
USER_AGENT = "ols-terms/0.1.0 (bio-tools; python-requests)"


class OLSError(RuntimeError):
    """Base error for OLS client / retrieval failures."""


class OLSNotFoundError(OLSError):
    """Raised when a term or ontology cannot be found (HTTP 404 or empty lookup)."""


def double_encode_iri(iri: str) -> str:
    """OLS4 requires term IRIs in URL paths to be double URL-encoded."""
    return quote(quote(iri, safe=""), safe="")


@dataclass
class TransportStats:
    """Counters for outbound traffic, used by the benchmark harness."""

    http_requests: int = 0
    bytes_downloaded: int = 0

    def reset(self) -> None:
        self.http_requests = 0
        self.bytes_downloaded = 0


class OLSClient:
    """Thin OLS4 REST client.

    - automatic retries with exponential backoff on 429/5xx and connection errors
    - client-side throttling (default <= 2 req/s)
    - request / byte counters in ``self.stats``
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self.stats = TransportStats()
        self._lock = threading.Lock()
        self._last_request_t = 0.0

        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()


            retry = CappedRetry(
                total=max_retries,
                connect=max_retries,
                read=max_retries,
                status=max_retries,
                backoff_factor=1.0,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET",),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval_s - (now - self._last_request_t)
            if wait > 0:
                time.sleep(wait)
            self._last_request_t = time.monotonic()

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET ``path`` (relative to base_url, or absolute URL) and return parsed JSON."""
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}/{path.lstrip('/')}"
        self._throttle()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        self.stats.http_requests += 1
        self.stats.bytes_downloaded += len(resp.content)
        if resp.status_code == 404:
            raise OLSNotFoundError(f"404 Not Found: {resp.url}")
        if not resp.ok:
            raise OLSError(f"HTTP {resp.status_code} from {resp.url}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise OLSError(f"Non-JSON response from {resp.url}") from exc
