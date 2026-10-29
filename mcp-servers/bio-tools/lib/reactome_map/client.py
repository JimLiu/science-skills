"""HTTP client for reactome.org with retries, politeness throttling and instrumentation.

All outbound traffic of the tool goes through :class:`ReactomeClient` so that the
benchmark can report exact request counts and bytes downloaded, and so the
provenance log records every request made on behalf of a mapping run.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

CONTENT_BASE = "https://reactome.org/ContentService"
ANALYSIS_BASE = "https://reactome.org/AnalysisService"


RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class RequestRecord:
    """One completed (possibly retried) logical HTTP request."""

    method: str
    url: str
    status: int
    elapsed_s: float
    bytes_received: int
    attempts: int


class ReactomeHTTPError(RuntimeError):
    """Raised when a request fails after exhausting retries."""


@dataclass
class ReactomeClient:
    """Small synchronous client for the Reactome ContentService / AnalysisService.

    Parameters
    ----------
    timeout:
        Per-request timeout in seconds.
    max_attempts:
        Maximum attempts per logical request (1 initial + retries).
    backoff_base_s:
        Base of the exponential backoff between retries (1s, 2s, 4s, ...).
    min_interval_s:
        Minimum spacing between outbound requests (politeness; default keeps
        the tool below 3 requests/second).
    transport:
        Optional ``httpx`` transport — used by the offline unit tests to mock
        the API without network access.
    """

    timeout: float = 60.0
    max_attempts: int = 4
    backoff_base_s: float = 1.0
    min_interval_s: float = 0.34
    user_agent: str = "reactome-map/1.0 (bio-tools wave 2)"
    transport: Any = None
    log: list[RequestRecord] = field(default_factory=list)
    _last_request_t: float = field(default=0.0, repr=False)
    _client: httpx.Client | None = field(default=None, repr=False)


    def _http(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": self.timeout,
                "headers": {"User-Agent": self.user_agent},
                "follow_redirects": True,
            }
            if self.transport is not None:
                kwargs["transport"] = self.transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "ReactomeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


    @property
    def http_requests(self) -> int:
        """Total outbound HTTP requests issued (each retry attempt counts)."""
        return sum(rec.attempts for rec in self.log)

    @property
    def bytes_downloaded(self) -> int:
        return sum(rec.bytes_received for rec in self.log)

    def reset_counters(self) -> None:
        self.log = []


    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        content: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue a request with throttling and retries; raise after final failure."""
        attempts = 0
        last_exc: Exception | None = None
        t0 = time.monotonic()
        while attempts < self.max_attempts:
            attempts += 1
            self._throttle()
            self._last_request_t = time.monotonic()
            try:
                resp = self._http().request(
                    method, url, params=params, content=content, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempts < self.max_attempts:
                    time.sleep(self.backoff_base_s * 2 ** (attempts - 1))
                continue
            if resp.status_code in RETRY_STATUS and attempts < self.max_attempts:
                time.sleep(self.backoff_base_s * 2 ** (attempts - 1))
                continue
            self.log.append(
                RequestRecord(
                    method=method,
                    url=str(resp.request.url),
                    status=resp.status_code,
                    elapsed_s=round(time.monotonic() - t0, 4),
                    bytes_received=len(resp.content),
                    attempts=attempts,
                )
            )
            if resp.status_code >= 400:
                raise ReactomeHTTPError(
                    f"{method} {resp.request.url} -> HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
            return resp
        raise ReactomeHTTPError(
            f"{method} {url} failed after {attempts} attempts: {last_exc!r}"
        )


    def database_version(self) -> str:
        """Reactome database release version (ContentService /data/database/version)."""
        resp = self.request("GET", f"{CONTENT_BASE}/data/database/version")
        return resp.text.strip()

    def mapping_pathways(
        self, resource: str, identifier: str, species: str = "9606"
    ) -> list[dict[str, Any]]:
        """ContentService low-level pathway mapping for one identifier.

        ``GET /data/mapping/{resource}/{identifier}/pathways?species=...``
        This is the per-gene "naive" pattern; the tool itself only uses it for
        optional cross-checks, never for the primary mapping.
        """
        resp = self.request(
            "GET",
            f"{CONTENT_BASE}/data/mapping/{urllib.parse.quote(str(resource), safe='')}/"
            f"{urllib.parse.quote(str(identifier), safe='')}/pathways",
            params={"species": species},
        )
        return resp.json()

    def query_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """ContentService batched object lookup (``POST /data/query/ids``), <=20 ids/call."""
        if len(ids) > 20:
            raise ValueError("ContentService /data/query/ids accepts at most 20 ids per call")
        resp = self.request(
            "POST",
            f"{CONTENT_BASE}/data/query/ids",
            content=",".join(ids),
            headers={"Content-Type": "text/plain"},
        )
        return resp.json()


    def analyse_identifiers(
        self,
        identifiers: list[str],
        *,
        sample_name: str = "reactome-map",
        interactors: bool = False,
        include_disease: bool = True,
        page_size: int = 20000,
        p_value: float = 1.0,
        projection: bool = False,
        resource: str = "TOTAL",
    ) -> dict[str, Any]:
        """Submit identifiers to the AnalysisService and return the full analysis result.

        ``POST /identifiers/`` (or ``/identifiers/projection`` when ``projection=True``)
        with a plain-text body (one identifier per line, ``#`` header line first).
        ``resource`` selects the molecule-resource view of the result
        (``TOTAL``, ``UNIPROT``, ``ENSEMBL``, ...): the AnalysisService filters the
        returned pathway list and reports resource-specific found counts.
        """
        path = "/identifiers/projection" if projection else "/identifiers/"
        body = "#" + sample_name + "\n" + "\n".join(identifiers) + "\n"
        resp = self.request(
            "POST",
            f"{ANALYSIS_BASE}{path}",
            params={
                "interactors": str(interactors).lower(),
                "includeDisease": str(include_disease).lower(),
                "pageSize": page_size,
                "page": 1,
                "sortBy": "ENTITIES_PVALUE",
                "order": "ASC",
                "resource": resource,
                "pValue": p_value,
            },
            content=body,
            headers={"Content-Type": "text/plain"},
        )
        return resp.json()

    def analysis_not_found(self, token: str, page_size: int = 20000) -> list[dict[str, Any]]:
        """Identifiers from a prior submission that could not be mapped (``GET /token/{t}/notFound``)."""
        resp = self.request(
            "GET",
            f"{ANALYSIS_BASE}/token/{urllib.parse.quote(str(token), safe='')}/notFound",
            params={"pageSize": page_size, "page": 1},
        )
        return resp.json()
