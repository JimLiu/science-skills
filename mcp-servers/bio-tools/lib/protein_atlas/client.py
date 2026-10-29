"""HTTP client for the Human Protein Atlas (proteinatlas.org).

Release pinning: HPA publishes each release on a versioned host
(v25.proteinatlas.org = release 25.x). The default base URL is the versioned
host so that battery results are immune to the annual release roll on
www.proteinatlas.org. Both hosts serve the same two surfaces:

  * per-gene JSON:  GET /{ENSG}.json   (flat dict of ~119 human-readable keys)
  * bulk search:    GET /api/search_download.php?search=...&format=json
                        &columns=<codes>&compress=no

Politeness: a configurable minimum interval between requests (default 0.5 s,
i.e. <= 2 req/s per the wave politeness rules), Retry-After honoured on 429,
exponential backoff on 5xx. The client counts outbound requests and
downloaded bytes so the benchmark can report absolute cost without
instrumenting the transport separately.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://v25.proteinatlas.org"
WWW_BASE_URL = "https://www.proteinatlas.org"
USER_AGENT = "bio-tools/protein-atlas-0.1.0"


class HpaError(RuntimeError):
    """Raised on a non-retryable HTTP error from proteinatlas.org."""

    def __init__(self, status_code: int, url: str, message: str = ""):
        super().__init__(f"HTTP {status_code} for {url}: {message}")
        self.status_code = status_code
        self.url = url


@dataclass
class _Counters:
    http_requests: int = 0
    bytes_downloaded: int = 0
    requests_log: list = field(default_factory=list)


class HpaClient:
    """Thin JSON client with rate limiting, retries and request accounting."""

    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = 0.5,
        max_retries: int = 5,
        timeout_s: float = 60.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_t = 0.0
        self.counters = _Counters()


    def reset_counters(self) -> None:
        self.counters = _Counters()


    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        """GET base_url+path with throttling, retries and accounting."""
        url = self.base_url + path
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            t0 = time.monotonic()
            resp = self.session.get(url, params=params, timeout=self.timeout_s)
            self._last_request_t = time.monotonic()
            self.counters.http_requests += 1
            self.counters.bytes_downloaded += len(resp.content)
            self.counters.requests_log.append(
                {"url": resp.url, "status": resp.status_code,
                 "bytes": len(resp.content),
                 "elapsed_s": round(self._last_request_t - t0, 3)})
            if resp.status_code in self.RETRYABLE and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = retry_after_seconds(retry_after, backoff)
                time.sleep(min(wait, 30.0))
                backoff *= 2
                continue
            return resp
        return resp

    def get_json(self, path: str, params: dict | None = None):
        resp = self.get(path, params=params)
        if resp.status_code != 200:
            raise HpaError(resp.status_code, resp.url, resp.text[:200])
        return resp.json()


    def gene_json(self, ensg: str) -> dict:
        """Full per-gene record: GET /{ENSG}.json (flat dict)."""
        obj = self.get_json(f"/{urllib.parse.quote(str(ensg), safe='')}.json")
        if not isinstance(obj, dict):
            raise HpaError(200, f"{self.base_url}/{ensg}.json",
                           "expected a JSON object")
        return obj

    def search_download(self, query: str, columns: str) -> list[dict]:
        """Column-selected bulk search: /api/search_download.php."""
        obj = self.get_json(
            "/api/search_download.php",
            params={"search": query, "format": "json",
                    "columns": columns, "compress": "no"})
        if not isinstance(obj, list):
            raise HpaError(200, f"{self.base_url}/api/search_download.php",
                           "expected a JSON array")
        return obj
