"""Instrumented, polite HTTP client for the Rfam REST API."""
from __future__ import annotations

import time

import requests

BASE_URL = "https://rfam.org"
USER_AGENT = "bio-tools-rfam-families/0.1 (anthropic-experimental/bio-tools)"
DEFAULT_TIMEOUT = 120.0
MIN_INTERVAL_S = 0.5


class RfamApiError(RuntimeError):
    """Non-2xx response from rfam.org (other than the cases below)."""

    def __init__(self, status_code: int, url: str, detail: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"Rfam API error {status_code} for {url}: {detail[:200]}")


class NotFound(RfamApiError):
    """404 — unknown family accession / id."""


class SearchUnavailable(RfamApiError):
    """The /search/sequence backend returned a server error.

    Observed 2026-06-08: valid submissions get an HTML 500 'Please come back
    later' page while input validation still works (bad sequences get a JSON
    400) — i.e. the cmscan job-submission backend is down, not the API.
    """


class RfamClient:
    """requests-backed client with politeness throttling and instrumentation.

    ``stats`` accumulates ``{"requests": n, "bytes": n}`` across all calls and
    is read by bench/run_bench.py; reset with :meth:`reset_stats`.
    """

    def __init__(self, session: requests.Session | None = None,
                 base_url: str = BASE_URL,
                 min_interval_s: float = MIN_INTERVAL_S,
                 timeout: float = DEFAULT_TIMEOUT):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout = timeout
        self._last_request_t = 0.0
        self.stats = {"requests": 0, "bytes": 0}

    def reset_stats(self) -> None:
        self.stats = {"requests": 0, "bytes": 0}



    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)
        self._last_request_t = time.monotonic()

    def _record(self, resp: requests.Response) -> None:
        self.stats["requests"] += 1
        self.stats["bytes"] += len(resp.content)

    def _raise_for(self, resp: requests.Response, url: str):
        if resp.status_code == 404:
            raise NotFound(404, url, "not found")
        raise RfamApiError(resp.status_code, url, resp.text[:500])



    def get_json(self, path: str) -> dict:
        """GET ``path`` with ``?content-type=application/json``."""
        url = f"{self.base_url}{path}"
        self._throttle()
        resp = self.session.get(url, params={"content-type": "application/json"},
                                timeout=self.timeout)
        self._record(resp)
        if not resp.ok:
            self._raise_for(resp, url)
        return resp.json()

    def get_text(self, path: str) -> str:
        """GET ``path`` with ``?content-type=text/plain``."""
        url = f"{self.base_url}{path}"
        self._throttle()
        resp = self.session.get(url, params={"content-type": "text/plain"},
                                timeout=self.timeout)
        self._record(resp)
        if not resp.ok:
            self._raise_for(resp, url)
        return resp.text

    def get_raw(self, path: str, params: dict | None = None,
                headers: dict | None = None) -> requests.Response:
        """GET without forcing a content-type parameter (used by the naive
        baseline in bench/ and by the search poller)."""
        url = f"{self.base_url}{path}"
        self._throttle()
        resp = self.session.get(url, params=params, headers=headers,
                                timeout=self.timeout)
        self._record(resp)
        return resp



    def submit_search(self, sequence: str) -> dict:
        """POST a nucleotide sequence to /search/sequence.

        Returns the submission payload (``resultURL``, ``jobId``, ``opened``).
        Raises :class:`SearchUnavailable` when the backend 500s on a valid
        sequence, :class:`RfamApiError` on a 400 (invalid sequence).
        """
        url = f"{self.base_url}/search/sequence"
        self._throttle()
        resp = self.session.post(url, data={"seq": sequence},
                                 headers={"Accept": "application/json"},
                                 timeout=self.timeout)
        self._record(resp)
        if resp.status_code >= 500:
            raise SearchUnavailable(resp.status_code, url,
                                    "sequence-search backend unavailable")
        if not resp.ok:
            self._raise_for(resp, url)
        return resp.json()

    def poll_search(self, result_url: str, max_wait_s: float = 300.0,
                    poll_interval_s: float = 5.0) -> dict:
        """Poll a search resultURL until HTTP 200 (done) or ``max_wait_s``."""
        deadline = time.monotonic() + max_wait_s
        while True:
            self._throttle()
            resp = self.session.get(result_url,
                                    headers={"Accept": "application/json"},
                                    timeout=self.timeout)
            self._record(resp)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in (202, 502, 503):
                self._raise_for(resp, result_url)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Rfam sequence search not finished after {max_wait_s}s "
                    f"({result_url})")
            time.sleep(poll_interval_s)
