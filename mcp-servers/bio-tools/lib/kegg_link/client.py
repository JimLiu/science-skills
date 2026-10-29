"""Rate-limited, instrumented HTTP client for the KEGG REST API (rest.kegg.jp).

KEGG asks API users for restraint; this client enforces a minimum interval between
requests (default 0.35 s, i.e. < 3 requests/second) and retries transient failures
(HTTP 5xx, 429, connection errors) with exponential backoff.  Every request is
counted and its downloaded bytes accumulated so benchmarks can report exact cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

DEFAULT_BASE_URL = "https://rest.kegg.jp"
USER_AGENT = "bio-tools/kegg-link 0.1.0 (batched /link,/conv,/find client)"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class RequestStats:
    """Counters for outbound HTTP traffic (including retries)."""

    requests: int = 0
    bytes_downloaded: int = 0
    paths: list = field(default_factory=list)

    def reset(self) -> None:
        self.requests = 0
        self.bytes_downloaded = 0
        self.paths = []


class KeggError(RuntimeError):
    """Raised when the KEGG REST API returns a non-retryable error or retries are exhausted."""


class KeggClient:
    """Thin GET client for rest.kegg.jp with politeness throttling and retries."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = 0.35,
        max_retries: int = 4,
        backoff_s: float = 1.0,
        timeout_s: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.stats = RequestStats()
        self._last_request_t = 0.0


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def get_text(self, path: str) -> str:
        """GET ``base_url + path`` and return the response body as text.

        KEGG returns 404 for queries with no result in some operations and 400 for
        malformed queries; 404 on /link and /conv simply means "no hits" and is
        returned to the caller as an empty string.
        """
        url = self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout_s)
                self._last_request_t = time.monotonic()
                self.stats.requests += 1
                self.stats.bytes_downloaded += len(resp.content)
                self.stats.paths.append(path)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (2 ** attempt))
                    continue
                raise KeggError(f"request failed after {attempt + 1} attempts: {url}") from exc

            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:

                return ""
            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                time.sleep(self.backoff_s * (2 ** attempt))
                continue
            raise KeggError(f"KEGG REST error {resp.status_code} for {url}: {resp.text[:200]!r}")

        raise KeggError(f"request failed after retries: {url}") from last_exc


    def info(self, database: str = "kegg") -> str:
        """Return the raw /info text for *database* (contains the KEGG release string)."""
        return self.get_text(f"/info/{quote(str(database), safe='')}")
