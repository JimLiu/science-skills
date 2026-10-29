"""Throttled, retrying GET client for the arXiv Atom API.

Politeness: the arXiv API terms of use ask for no more than one request
every 3 seconds from a client — one client instance enforces that interval.
Budget discipline: request timeout 20 s, at most ONE retry on 429/5xx or
transport errors (a retry costs an extra pacing sleep too, and the whole
tool call must stay < 50 s).
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

import requests

from mcp_servers_common.ua import product_ua

BASE_URL = "https://export.arxiv.org/api/query"



USER_AGENT = product_ua(include_email=False)


class ArxivApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class ArxivClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 3.0,
                 timeout_s: float = 20.0, max_attempts: int = 2,
                 session: requests.Session | None = None):
        self.base_url = base_url
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def query(self, params: dict) -> ET.Element:
        """GET the query endpoint; return the parsed Atom feed root."""
        last_err: Exception | None = None
        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                resp = self.session.get(self.base_url, params=params,
                                        timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                continue
            self._last_request_t = time.monotonic()
            if resp.status_code in self.RETRY_STATUSES:
                last_err = ArxivApiError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            if resp.status_code != 200:
                raise ArxivApiError(
                    f"HTTP {resp.status_code}: {resp.text[:300]}")




            if "<!DOCTYPE" in resp.text:
                raise ArxivApiError("unexpected DTD in arXiv response")
            try:
                return ET.fromstring(resp.text)
            except ET.ParseError as exc:
                last_err = exc
                continue
        raise ArxivApiError(
            f"giving up after {self.max_attempts} attempts: {last_err!r}")
