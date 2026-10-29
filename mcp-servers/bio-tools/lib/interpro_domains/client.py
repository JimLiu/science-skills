"""HTTP client for the InterPro REST API with complete pagination, retries and rate limiting.

Endpoints used
--------------
Forward (protein -> InterPro entries), the primary endpoint of this tool:
    GET /api/entry/interpro/protein/uniprot/<acc>/?page_size=N
    Response: {"count": int, "next": url|null, "previous": url|null, "results": [...]}
    Pagination is followed via the "next" URL until it is null.

Independent count (accuracy gate ground truth):
    GET /api/protein/uniprot/<acc>/?extra_fields=counters
    Response metadata.counters.dbEntries.interpro is the number of InterPro entries
    that match the protein, computed by a different query path on the server.

Inverse pair check (accuracy gate cross-check):
    GET /api/protein/uniprot/<acc>/entry/interpro/<entry_acc>/
    Response: {"metadata": {...protein...}, "entries": [{accession, entry_protein_locations, ...}]}

Notes
-----
* HTTP 204 (No Content) means the protein exists but has no matching InterPro entries.
* HTTP 404 means the accession is unknown to InterPro.
* Retries: 408/429/5xx and transport errors are retried with exponential backoff.
* Rate limiting: a minimum interval between requests is enforced (default 0.25 s = 4 req/s,
  below the 5 req/s shared-EBI-host policy used for this build wave).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from mcp_servers_common.pathseg import seg
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/interpro/api"
USER_AGENT = "interpro-domains/0.1 (bio-tools; batch domain-architecture retrieval)"

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class InterProError(RuntimeError):
    """Raised for non-retryable API failures."""


class AccessionNotFound(InterProError):
    """Raised when InterPro returns 404 for a UniProt accession."""


@dataclass
class RequestStats:
    """Counters for outbound traffic, used by the benchmark harness."""

    requests: int = 0
    bytes_downloaded: int = 0
    urls: list = field(default_factory=list)

    def record(self, url: str, n_bytes: int) -> None:
        self.requests += 1
        self.bytes_downloaded += n_bytes
        self.urls.append(url)


class InterProClient:
    """Small synchronous client for the InterPro REST API.

    Parameters
    ----------
    base_url:
        API root (default: the public EBI endpoint).
    page_size:
        Page size requested from the API for paginated listings (default 200; the API
        honors at least 200 — verified against the global entry listing).
    min_interval_s:
        Minimum spacing between outbound requests (rate limiting), default 0.25 s.
    max_retries:
        Maximum number of retries per request for retryable failures.
    timeout_s:
        Per-request timeout.
    transport:
        Optional httpx transport (used by offline tests with httpx.MockTransport).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = 200,
        min_interval_s: float = 0.25,
        max_retries: int = 5,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.stats = RequestStats()
        self._last_request_t = 0.0
        self._client = httpx.Client(
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=timeout_s,
            transport=transport,
            follow_redirects=True,
        )



    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "InterProClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _get(self, url: str) -> httpx.Response | None:
        """GET with throttling and retries.  Returns None for HTTP 204 (empty result)."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url)
                self._last_request_t = time.monotonic()
                self.stats.record(url, len(resp.content))
            except httpx.HTTPError as exc:
                self._last_request_t = time.monotonic()
                last_exc = exc
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            if resp.status_code == 204:
                return None
            if resp.status_code == 404:
                raise AccessionNotFound(f"404 Not Found: {url}")
            if resp.status_code in RETRYABLE_STATUS:
                last_exc = InterProError(f"HTTP {resp.status_code} from {url}")
                if attempt < self.max_retries:
                    self._backoff(attempt, resp)
                continue
            resp.raise_for_status()
            return resp
        raise InterProError(f"Exhausted {self.max_retries} retries for {url}: {last_exc}")

    @staticmethod
    def _backoff(attempt: int, resp: httpx.Response | None = None) -> None:
        delay = min(2.0**attempt, 30.0)
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            delay = max(delay, retry_after_seconds(retry_after, 0.0))
        time.sleep(delay)

    def _get_json(self, url: str) -> dict | None:
        resp = self._get(url)
        if resp is None:
            return None
        return resp.json()



    def get_protein_entries(self, accession: str, page_size: int | None = None) -> dict:
        """Fetch ALL InterPro entries matching a UniProt accession (complete pagination).

        Returns ``{"accession", "count", "results", "pages"}`` where ``results`` is the
        concatenation of every page's ``results`` list and ``pages`` is the number of
        pages traversed.  Raises ``InterProError`` if the number of accumulated results
        does not equal the API's ``count`` field.
        """
        ps = page_size or self.page_size
        acc = seg(accession)
        url = f"{self.base_url}/entry/interpro/protein/uniprot/{acc}/?page_size={ps}"
        results: list = []
        count = 0
        pages = 0
        while url:
            payload = self._get_json(url)
            pages += 1
            if payload is None:
                count = 0
                break
            count = payload.get("count", 0)
            results.extend(payload.get("results", []))
            url = payload.get("next")
        if len(results) != count:
            raise InterProError(
                f"{accession}: pagination incomplete — accumulated {len(results)} results "
                f"but API count is {count}"
            )
        return {"accession": accession, "count": count, "results": results, "pages": pages}

    def get_protein_entries_first_page_only(self, accession: str) -> dict:
        """The naive baseline: a single unpaginated request with the API default page size (20).

        Returns ``{"accession", "count", "results", "raw"}`` — ``raw`` is the verbatim
        response body text (what an agent ingesting raw JSON would read).
        """
        url = (f"{self.base_url}/entry/interpro/protein/uniprot/"
               f"{seg(accession)}/")
        resp = self._get(url)
        if resp is None:
            return {"accession": accession, "count": 0, "results": [], "raw": ""}
        payload = resp.json()
        return {
            "accession": accession,
            "count": payload.get("count", 0),
            "results": payload.get("results", []),
            "raw": resp.text,
        }

    def get_protein_entries_raw_paginated(self, accession: str) -> dict:
        """Complete pagination with the API default page size, keeping verbatim page bodies.

        This is the 'manual but correct' raw-JSON workflow an agent would follow without
        this tool: default page size, follow ``next`` links, ingest every raw page.
        Returns ``{"accession", "count", "results", "raw_pages"}``.
        """
        url = (f"{self.base_url}/entry/interpro/protein/uniprot/"
               f"{seg(accession)}/")
        results: list = []
        raw_pages: list[str] = []
        count = 0
        while url:
            resp = self._get(url)
            if resp is None:
                break
            payload = resp.json()
            raw_pages.append(resp.text)
            count = payload.get("count", 0)
            results.extend(payload.get("results", []))
            url = payload.get("next")
        return {"accession": accession, "count": count, "results": results, "raw_pages": raw_pages}

    def get_protein_interpro_count(self, accession: str) -> int:
        """Independent ground-truth count of InterPro entries for a protein.

        Uses ``/protein/uniprot/<acc>/?extra_fields=counters`` and reads
        ``metadata.counters.dbEntries.interpro`` (0 if absent).
        """
        url = (f"{self.base_url}/protein/uniprot/"
               f"{seg(accession)}/?extra_fields=counters")
        payload = self._get_json(url)
        if payload is None:
            return 0
        counters = payload.get("metadata", {}).get("counters", {})
        return int(counters.get("dbEntries", {}).get("interpro", 0))

    def get_protein_entry_pair(self, accession: str, entry_accession: str) -> dict | None:
        """Inverse cross-check: protein-centric endpoint filtered by one InterPro entry.

        ``GET /protein/uniprot/<acc>/entry/interpro/<entry_acc>/`` returns the protein
        metadata plus an ``entries`` list containing that entry with its locations on the
        protein, or 404/204 if the pair does not exist.  Returns the matching entry dict
        or None.
        """
        url = (f"{self.base_url}/protein/uniprot/"
               f"{seg(accession)}/entry/interpro/"
               f"{seg(entry_accession)}/")
        try:
            payload = self._get_json(url)
        except AccessionNotFound:
            return None
        if payload is None:
            return None
        for entry in payload.get("entries", []):
            if entry.get("accession", "").upper() == entry_accession.upper():
                return entry
        return None


def fetch_domain_architecture(
    accessions: list[str],
    client: InterProClient | None = None,
    page_size: int | None = None,
) -> dict:
    """Fetch complete, deterministic domain-architecture summaries for a list of accessions.

    Returns ``{"summaries": {acc: summary_dict, ...}, "stats": {...}}``.
    Accessions are processed in the given order; each summary is deterministic
    (see :mod:`interpro_domains.summary`).
    """
    from .summary import build_summary

    own_client = client is None
    client = client or InterProClient()
    try:
        summaries = {}
        for acc in accessions:
            fetched = client.get_protein_entries(acc, page_size=page_size)
            summaries[acc] = build_summary(acc, fetched["count"], fetched["results"])
        return {
            "summaries": summaries,
            "stats": {
                "http_requests": client.stats.requests,
                "bytes_downloaded": client.stats.bytes_downloaded,
            },
        }
    finally:
        if own_client:
            client.close()
