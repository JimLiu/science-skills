"""HTTP client for the ENCODE portal (www.encodeproject.org) JSON API.

Behaviors verified live 2026-06-08:
  * Every route accepts format=json.
  * /search/ IGNORES the `from` parameter (encodeD limitation) -- pages cannot be
    walked there. /report/ honors `from`+`limit` and, with an explicit
    `sort=accession`, a from-walk returns exactly the same accession set as
    `limit=all` on /search/. All paged retrieval therefore goes through /report/.
  * Zero-hit searches return HTTP 404 with a JSON body
    {"total": 0, "@graph": [], "notification": "No results found"} -- the client
    converts that to an empty result instead of raising.
  * Default page size without `limit` is 25 rows (the naive-baseline trap).
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any

import requests

BASE_URL = "https://www.encodeproject.org"
USER_AGENT = "bio-tools/encode-search/1.0 (research)"


class EncodeAPIError(RuntimeError):
    """Raised on non-2xx responses that are not the documented zero-hit 404."""

    def __init__(self, status_code: int, url: str, detail: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"ENCODE API error {status_code} for {url}: {detail[:200]}")


@dataclasses.dataclass
class Stats:
    requests: int = 0
    bytes_downloaded: int = 0


class EncodeClient:
    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 60.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.stats = Stats()
        self._last_request_t = 0.0
        self._session = session or requests.Session()
        self._session.headers.update({"Accept": "application/json",
                                      "User-Agent": USER_AGENT})

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def get_json(self, path: str, params: dict[str, Any] | None = None,
                 allow_empty_404: bool = False) -> dict:
        """GET a JSON document. `params` may contain repeated keys via list values."""
        self._throttle()
        url = self.base_url + path
        resp = self._session.get(url, params=params, timeout=self.timeout_s)
        self._last_request_t = time.monotonic()
        self.stats.requests += 1
        self.stats.bytes_downloaded += len(resp.content)
        if resp.status_code == 404 and allow_empty_404:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("total") == 0:
                return body
        if resp.status_code != 200:
            raise EncodeAPIError(resp.status_code, resp.url, resp.text)
        return resp.json()
