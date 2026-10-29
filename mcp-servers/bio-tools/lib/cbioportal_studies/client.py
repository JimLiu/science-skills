"""Throttled, lightly-retrying HTTP client for the cBioPortal public REST API.

Upstream: https://www.cbioportal.org/api (keyless, OpenAPI-documented).

Conventions verified live 2026-06-10:
  * Listing GETs take pageSize/pageNumber (0-based) + projection
    (META/SUMMARY/DETAILED). projection=META returns an empty body and the
    collection's true size in the ``total-count`` response header.
  * The default/maximum pageSize is 10,000,000 — one request with
    pageSize=10000000 returns a complete collection (no server-side cap
    below that), so fetches here are complete by construction.
  * Gene-filtered retrieval is POST-with-JSON-filter: the GET variants
    silently IGNORE unknown query params (e.g. entrezGeneId on
    /discrete-copy-number), so mutations and CNAs must go through
    ``/mutations/fetch`` and ``/discrete-copy-number/fetch``.
  * POST /sample-lists/fetch and POST /studies/fetch silently DROP unknown
    ids from the response (no error) — callers must diff requested vs
    returned ids.
  * 404s carry ``{"message": "..."}`` JSON.

Politeness: one client instance enforces >= 0.5 s between requests
(<= 2 req/s). Budget: timeout 20 s per request, at most ONE retry on
429/5xx/transport errors with a short (<= 5 s) backoff — worst case stays
well under the 50 s per-tool wall-clock budget for single-digit-request
tools.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

import requests

from mcp_servers_common.ratelimit import retry_after_seconds

API_BASE = "https://www.cbioportal.org/api"
MIN_REQUEST_INTERVAL = 0.5
TIMEOUT = 20.0
MAX_ATTEMPTS = 2
RETRY_STATUSES = {429, 500, 502, 503, 504}

PAGE_ALL = 10_000_000

__version__ = "0.1.0"


class CBioPortalError(RuntimeError):
    """Unrecoverable upstream or transport error."""


class NotFound(CBioPortalError):
    """The API reported the requested entity does not exist (HTTP 404)."""


def seg(value: Any) -> str:
    """Percent-encode one URL path segment (fleet convention: quote safe='').

    Every caller-controlled identifier interpolated into a request path goes
    through this — nothing can smuggle '/', '?', '#' or '..' into the route.
    """
    return urllib.parse.quote(str(value), safe="")


class CBioPortalClient:
    """Paced JSON client; every call returns (parsed_body, total_count|None)."""

    def __init__(self, base_url: str = API_BASE,
                 session: requests.Session | None = None,
                 min_interval: float = MIN_REQUEST_INTERVAL,
                 timeout: float = TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", f"bio-tools-cbioportal-studies/{__version__}")
        self.min_interval = min_interval
        self.timeout = timeout
        self.http_requests = 0
        self.bytes_downloaded = 0
        self._last_request_at = 0.0



    def _request(self, method: str, path: str,
                 params: dict[str, Any] | None = None,
                 body: Any | None = None) -> tuple[Any, int | None]:
        url = self.base_url + path
        last_err: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.request(method, url, params=params,
                                            json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                last_err = exc
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(2.0)
                continue
            self._last_request_at = time.monotonic()
            self.http_requests += 1
            self.bytes_downloaded += len(resp.content)
            if resp.status_code in RETRY_STATUSES:
                last_err = CBioPortalError(
                    f"HTTP {resp.status_code} on {path}: {resp.text[:200]}")
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, 2.0, cap=5.0)
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(delay)
                continue
            if resp.status_code == 404:
                msg = path
                try:
                    msg = resp.json().get("message", path)
                except ValueError:
                    pass
                raise NotFound(msg)
            if resp.status_code != 200:
                raise CBioPortalError(
                    f"HTTP {resp.status_code} on {path}: {resp.text[:300]}")
            total = resp.headers.get("total-count")
            total_count = int(total) if total is not None and total.isdigit() else None
            if not resp.content:

                return None, total_count
            try:
                return resp.json(), total_count
            except ValueError as exc:
                raise CBioPortalError(
                    f"non-JSON body from {path}: {resp.text[:200]}") from exc
        raise CBioPortalError(
            f"giving up on {method} {path} after {MAX_ATTEMPTS} attempts: "
            f"{last_err!r}")



    def get(self, path: str,
            params: dict[str, Any] | None = None) -> tuple[Any, int | None]:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Any,
             params: dict[str, Any] | None = None) -> tuple[Any, int | None]:
        return self._request("POST", path, params=params, body=body)

    def meta_count(self, path: str, params: dict[str, Any] | None = None,
                   body: Any | None = None) -> int:
        """The collection's true size via projection=META (total-count header)."""
        p = dict(params or {})
        p["projection"] = "META"
        if body is None:
            _, total = self._request("GET", path, params=p)
        else:
            _, total = self._request("POST", path, params=p, body=body)
        if total is None:
            raise CBioPortalError(
                f"no total-count header on META request to {path}")
        return total
