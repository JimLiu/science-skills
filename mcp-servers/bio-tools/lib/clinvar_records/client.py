"""Throttled, retrying E-utilities client for the ClinVar database.

Politeness per NCBI etiquette: tool= and email= identification on every
request and a minimum interval between requests (default 0.4 s -> <= 2.5
req/s, under NCBI's keyless 3 req/s ceiling). Budget discipline per the
MCP 60 s transport limit: short timeout (default 30 s) and at most ONE
retry on 429/5xx/transport errors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TOOL = "mcp-bio"
DEFAULT_MIN_INTERVAL_S = 0.4
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_RETRIES = 1
RETRY_STATUS = {429, 500, 502, 503, 504}


BACKOFF_S = 2.0


class ClinVarApiError(RuntimeError):
    """Unrecoverable E-utilities failure (after retry) or malformed payload."""


@dataclass
class RequestStats:
    requests: int = 0
    bytes_downloaded: int = 0


class ClinVarClient:
    """Paced esearch/esummary wrapper for ``db=clinvar`` (JSON retmode)."""

    def __init__(self, email: str, tool: str = DEFAULT_TOOL,
                 api_key: str | None = None,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 session: requests.Session | None = None) -> None:
        if not email or "@" not in email:
            raise ValueError("NCBI etiquette requires a contact email address")
        self.email = email
        self.tool = tool
        self.api_key = api_key
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": f"{tool}/0.1 ({email}; python-requests)"})
        self.stats = RequestStats()



    def _common_params(self) -> dict:
        params = {"tool": self.tool, "email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _throttle(self) -> None:



        pace(EUTILS_BASE, self.min_interval_s)

    def _request(self, endpoint: str, data: dict) -> dict:
        """POST to ``{EUTILS_BASE}/{endpoint}``; return the parsed JSON body.

        POST keeps long id= lists off the URL. Retries at most
        ``max_retries`` times on 429/5xx/transport faults, honouring
        Retry-After when present.
        """
        url = f"{EUTILS_BASE}/{endpoint}"
        payload = {**self._common_params(), **data, "retmode": "json"}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.post(url, data=payload,
                                         timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(BACKOFF_S)
                    continue
                raise ClinVarApiError(f"{endpoint} transport failure: {exc}")
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code in RETRY_STATUS:
                last_err = ClinVarApiError(
                    f"{endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < self.max_retries:
                    retry_after = (resp.headers.get("Retry-After") or "").strip()
                    time.sleep(retry_after_seconds(retry_after, BACKOFF_S,
                                                   cap=10.0))
                    continue
                raise last_err
            if resp.status_code != 200:
                raise ClinVarApiError(
                    f"{endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise ClinVarApiError(
                    f"{endpoint} returned non-JSON: {resp.text[:200]}")
        raise ClinVarApiError(f"{endpoint} retries exhausted: {last_err!r}")



    def esearch(self, term: str, retmax: int = 20, retstart: int = 0) -> dict:
        """esearch db=clinvar; returns the ``esearchresult`` dict
        (``count`` is the API's own total as a string, ``idlist`` the
        page of variation-ID UIDs)."""
        body = self._request("esearch.fcgi", {
            "db": "clinvar", "term": term,
            "retmax": retmax, "retstart": retstart})
        result = body.get("esearchresult")
        if result is None:
            raise ClinVarApiError(f"esearch missing esearchresult: {body}")
        if "ERROR" in result:
            raise ClinVarApiError(f"esearch error: {result['ERROR']}")
        return result

    def esummary(self, uids: list[str]) -> dict:
        """esummary db=clinvar for a batch of UIDs; returns the ``result``
        dict keyed by UID (plus the ``uids`` order list)."""
        if not uids:
            return {"uids": []}
        body = self._request("esummary.fcgi", {
            "db": "clinvar", "id": ",".join(str(u) for u in uids)})
        result = body.get("result")
        if result is None:
            raise ClinVarApiError(f"esummary missing result: {body}")
        return result
