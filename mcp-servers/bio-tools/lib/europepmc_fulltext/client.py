"""Instrumented, rate-limited HTTP client for the Europe PMC REST service.

Politeness: <= 2 requests/second (min 0.5 s between requests), retries with
exponential backoff on 429/5xx and connection errors. 404 is NOT retried --
on the fullTextXML endpoint it is the meaningful "no full text in the OA
subset" signal.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field

import requests
from mcp_servers_common.ratelimit import retry_after_seconds

BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
USER_AGENT = "europepmc-fulltext/0.1 (bio-tools; contact: bio-tools maintainers)"

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class EuropePMCClient:
    base_url: str = BASE_URL
    min_interval_s: float = 0.5




    max_retries: int = 1
    timeout_s: float = 30.0

    n_requests: int = 0
    bytes_downloaded: int = 0
    _last_request_t: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET with throttling, instrumentation and retries on transient errors."""
        attempt = 0
        while True:
            self._throttle()
            try:
                self._last_request_t = time.monotonic()
                resp = self._session.get(url, params=params, timeout=self.timeout_s)
                self.n_requests += 1
                self.bytes_downloaded += len(resp.content)
            except (requests.ConnectionError, requests.Timeout) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                time.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                delay = retry_after_seconds(retry_after, min(2 ** attempt, 10), cap=10.0)
                time.sleep(delay)
                continue
            return resp


    def search(self, query: str, result_type: str = "core", page_size: int = 100) -> dict:
        """One page of /search as parsed JSON (the tool's batched availability
        queries always fit on one page)."""
        resp = self._get(
            f"{self.base_url}/search",
            params={
                "query": query,
                "format": "json",
                "resultType": result_type,
                "pageSize": page_size,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def full_text_xml(self, pmcid: str) -> tuple[int, bytes | None]:
        """GET /{PMCID}/fullTextXML. Returns (status_code, xml_bytes or None).

        404 means the article is not in the Europe PMC open-access full-text
        subset (even if a PMCID exists) -- returned as (404, None), not raised.
        """
        resp = self._get(
            f"{self.base_url}/{urllib.parse.quote(str(pmcid), safe='')}/fullTextXML")
        if resp.status_code == 200:
            return 200, resp.content
        if resp.status_code == 404:
            return 404, None
        resp.raise_for_status()
        return resp.status_code, None
