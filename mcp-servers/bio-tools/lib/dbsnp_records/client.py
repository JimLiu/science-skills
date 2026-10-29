"""Throttled, retrying HTTP client for dbSNP (two NCBI hosts).

- ``api.ncbi.nlm.nih.gov/variation/v0`` — RefSNP JSON (no key needed).
- ``eutils.ncbi.nlm.nih.gov`` — esearch db=snp for region queries, with
  tool=/email= identification per NCBI etiquette.

Both hosts share ONE process-wide pacing budget (default 0.4 s min
interval -> <= 2.5
req/s, under NCBI's keyless 3 req/s ceiling). Budget discipline per the MCP
60 s transport limit: short timeouts and at most ONE retry on
429/5xx/transport errors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

VARIATION_BASE = "https://api.ncbi.nlm.nih.gov/variation/v0"
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TOOL = "mcp-bio"
DEFAULT_MIN_INTERVAL_S = 0.4
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_RETRIES = 1
RETRY_STATUS = {429, 500, 502, 503, 504}

BACKOFF_S = 2.0


class DbsnpApiError(RuntimeError):
    """Unrecoverable dbSNP API failure (after retry) or malformed payload."""


@dataclass
class RequestStats:
    requests: int = 0
    bytes_downloaded: int = 0


class DbsnpClient:
    """Paced GET client over Variation Services + esearch db=snp."""

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

    def _throttle(self, url: str) -> None:




        pace(url, self.min_interval_s)

    def _request(self, url: str, params: dict | None = None,
                 data: dict | None = None,
                 ok_404: bool = False) -> requests.Response | None:
        """Paced GET (or POST when ``data`` is given) with <= max_retries
        retries. POST keeps identification params — including any api_key —
        in the request body, out of URLs and exception texts. Returns None
        on 404 when ``ok_404`` (entity absent), raises DbsnpApiError
        otherwise."""
        method = "POST" if data is not None else "GET"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            try:
                if data is not None:
                    resp = self.session.post(url, data=data,
                                             timeout=self.timeout_s)
                else:
                    resp = self.session.get(url, params=params,
                                            timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(BACKOFF_S)
                    continue
                raise DbsnpApiError(f"{method} {url} transport failure: {exc}")
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code == 404 and ok_404:
                return None
            if resp.status_code in RETRY_STATUS:
                last_err = DbsnpApiError(
                    f"{method} {url} HTTP {resp.status_code}: "
                    f"{resp.text[:200]}")
                if attempt < self.max_retries:
                    retry_after = (resp.headers.get("Retry-After") or "").strip()
                    time.sleep(retry_after_seconds(retry_after, BACKOFF_S,
                                                   cap=10.0))
                    continue
                raise last_err
            if resp.status_code != 200:
                raise DbsnpApiError(
                    f"{method} {url} HTTP {resp.status_code}: "
                    f"{resp.text[:200]}")
            return resp
        raise DbsnpApiError(f"{method} {url} retries exhausted: {last_err!r}")



    def get_refsnp(self, rs_number: int) -> dict | None:
        """Fetch one RefSNP JSON object; None when the rs number does not
        exist (HTTP 404)."""
        resp = self._request(f"{VARIATION_BASE}/refsnp/{rs_number}",
                             ok_404=True)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            raise DbsnpApiError(
                f"refsnp/{rs_number} returned non-JSON: {resp.text[:200]}")



    def esearch_snp(self, term: str, retmax: int) -> dict:
        """esearch db=snp; returns the ``esearchresult`` dict (``count`` is
        the API's own total, ``idlist`` bare rs numbers). Sent as POST so
        the api_key (when configured) stays in the body — never in a URL
        that could leak via exception text (precedent: pubmed_search)."""
        data = {"db": "snp", "term": term, "retmax": retmax,
                "retmode": "json", "tool": self.tool, "email": self.email}
        if self.api_key:
            data["api_key"] = self.api_key
        resp = self._request(f"{EUTILS_BASE}/esearch.fcgi", data=data)
        try:
            body = resp.json()
        except ValueError:
            raise DbsnpApiError(f"esearch returned non-JSON: {resp.text[:200]}")
        result = body.get("esearchresult")
        if result is None:
            raise DbsnpApiError(f"esearch missing esearchresult: {body}")
        if "ERROR" in result:
            raise DbsnpApiError(f"esearch error: {result['ERROR']}")
        return result
