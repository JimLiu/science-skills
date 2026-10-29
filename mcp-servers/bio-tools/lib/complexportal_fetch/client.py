"""HTTP client for the EBI Complex Portal web service.

Politeness: <= 2 requests/s on the shared EBI host (min 0.5 s between requests),
identifying User-Agent, retries with exponential backoff on 429/5xx/transport errors.
"""
from __future__ import annotations

import time
import urllib.parse

import httpx
from mcp_servers_common.ratelimit import retry_after_seconds

BASE_URL = "https://www.ebi.ac.uk/intact/complex-ws"
USER_AGENT = "complexportal-fetch/0.1 (bio-tools benchmark suite)"
MIN_INTERVAL_S = 0.5
RETRY_STATUS = {429, 500, 502, 503, 504}


class ComplexPortalError(RuntimeError):
    """Raised when the web service returns an unrecoverable error."""


class NotFoundError(ComplexPortalError):
    """Raised when a complex accession does not exist (HTTP 404)."""


class ComplexPortalClient:
    """Thin HTTP wrapper with throttling, retries and request accounting."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 4,
        min_interval_s: float = MIN_INTERVAL_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self._last_request_t = 0.0

        self.request_count = 0
        self.bytes_downloaded = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )


    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ComplexPortalClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET ``base_url + path`` and return the parsed JSON body.

        Retries on 429/5xx and transport errors with exponential backoff.
        Raises NotFoundError on 404 and ComplexPortalError on other failures.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url, params=params)
                self._last_request_t = time.monotonic()
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
            except httpx.TransportError as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(min(2.0 * 2 ** attempt, 30.0))
                    continue
                raise ComplexPortalError(f"transport error for {url}: {exc}") from exc

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise NotFoundError(f"not found: {url}")
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")



                wait = retry_after_seconds(
                    retry_after, min(2.0 * 2 ** attempt, 30.0))
                time.sleep(wait)
                last_err = ComplexPortalError(f"HTTP {resp.status_code} for {url}")
                continue
            raise ComplexPortalError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
        raise ComplexPortalError(f"retries exhausted for {url}: {last_err}")



    def get_complex(self, complex_ac: str) -> dict:
        """Full single-complex record from /complex/{AC}."""
        return self.get_json(f"complex/{urllib.parse.quote(complex_ac, safe='')}")

    def get_complex_simplified(self, complex_ac: str) -> dict:
        """Reduced single-complex record from /complex-simplified/{AC} (used by the gate)."""
        return self.get_json(f"complex-simplified/{urllib.parse.quote(complex_ac, safe='')}")

    def get_export(self, complex_ac: str) -> dict:
        """MI-JSON export of a complex from /export/{AC} (used by the gate)."""
        return self.get_json(f"export/{urllib.parse.quote(complex_ac, safe='')}")

    def search(self, query: str, first: int = 0, number: int | None = None,
               filters: str | None = None, facets: str | None = None) -> dict:
        """Solr search via /search/{query}. Returns the raw response dict."""
        params: dict = {"format": "json", "first": first}
        if number is not None:
            params["number"] = number
        if filters:
            params["filters"] = filters
        if facets:
            params["facets"] = facets
        return self.get_json(f"search/{urllib.parse.quote(query, safe='')}", params=params)
