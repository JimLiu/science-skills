"""Throttled, retrying HTTP client for PubChem PUG REST / PUG-View.

Politeness (NCBI usage policy): a hard minimum interval between requests
(default 0.5 s -> <= 2 req/s, well under NCBI's documented 5 req/s ceiling)
and ``tool``/``email`` identification params on every request, mirroring the
E-utilities etiquette used by ``pubmed_fetch``.

Budget: per-request timeout 15 s and at most ONE retry (429/5xx/transport)
with a short backoff, so a single request costs < 35 s worst case — tools
composed of <= 2 requests stay under the MCP transport deadline.

PubChem signals "no match" as HTTP 404 with a ``Fault`` JSON body
(``PUGREST.NotFound``); that is raised as :class:`NotFound`, never retried.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds
from mcp_servers_common.ua import contact_email, product_ua

PUG_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUG_VIEW_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"
USER_AGENT = product_ua()
DEFAULT_TOOL = "bio-tools-pubchem-compounds"



DEFAULT_EMAIL = os.environ.get("NCBI_EMAIL") or contact_email()

RETRY_STATUS = {429, 500, 502, 503, 504}


class PubChemApiError(RuntimeError):
    """Unrecoverable PUG REST error (bad request, schema change, exhausted retries)."""


class NotFound(PubChemApiError):
    """PUG REST reported no record for the identifier (PUGREST.NotFound)."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0
    per_url: list = field(default_factory=list)


class PugRestClient:
    """Paced GET/POST client returning parsed JSON payloads."""

    def __init__(self, min_interval_s: float = 0.5, timeout_s: float = 15.0,
                 max_retries: int = 1, tool: str = DEFAULT_TOOL,
                 email: str | None = DEFAULT_EMAIL,
                 session: requests.Session | None = None):
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.tool = tool
        self.email = email
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.stats = TransportStats()

    def _throttle(self) -> None:




        pace(PUG_BASE, self.min_interval_s)

    def request_json(self, url: str, params: dict | None = None,
                     data: dict | None = None) -> dict:
        """GET (or POST when ``data`` is given) a PUG REST URL; return JSON.

        Raises NotFound on a PUGREST.NotFound fault, PubChemApiError on any
        other fault or after exhausting the single retry.
        """

        ident: dict[str, str] = {"tool": self.tool}
        if self.email:
            ident["email"] = self.email
        params = {**ident, **(params or {})}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                if data is not None:
                    resp = self.session.post(url, params=params, data=data,
                                             timeout=self.timeout_s)
                else:
                    resp = self.session.get(url, params=params,
                                            timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2.0)
                continue
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            self.stats.per_url.append({"url": url, "status": resp.status_code,
                                       "bytes": len(resp.content)})
            if resp.status_code in RETRY_STATUS:
                last_err = PubChemApiError(f"HTTP {resp.status_code}")
                delay = retry_after_seconds(resp.headers.get("Retry-After", ""),
                                            2.0, cap=10.0)
                if attempt < self.max_retries:
                    time.sleep(delay)
                continue
            if resp.status_code == 404:

                try:
                    fault = resp.json().get("Fault", {})
                except json.JSONDecodeError:
                    fault = {}
                raise NotFound(fault.get("Message") or "no record found")
            if resp.status_code != 200:
                try:
                    fault = resp.json().get("Fault", {})
                    detail = "; ".join(fault.get("Details", [])) or fault.get("Message", "")
                except json.JSONDecodeError:
                    detail = resp.text[:300]
                raise PubChemApiError(f"HTTP {resp.status_code}: {detail}")
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise PubChemApiError(f"non-JSON response from {url}: {exc}") from exc
        raise PubChemApiError(
            f"giving up after {self.max_retries + 1} attempts: {last_err!r}")
