"""Batched PubMed retrieval via NCBI E-utilities (epost + efetch).

Legacy pattern being replaced::

    from Bio import Entrez
    for pmid in pmids:                                  # one HTTP request per PMID
        handle = Entrez.efetch(db="pubmed", id=pmid, rettype="xml")
        xml = handle.read()

Modern pattern::

    with PubMedFetcher(email="you@example.org") as pf:  # email-literal-allow (docstring example)
        xml_by_pmid = pf.fetch_xml(pmids)        # 1 epost + ceil(N/200) efetch requests
        records     = pf.fetch_records(pmids)    # structured JSON-able dicts

All requests are form-encoded POSTs to eutils.ncbi.nlm.nih.gov with tool/email
identification, a politeness sleep before every request (default 0.5 s, i.e. <= 2 req/s),
and retries with exponential backoff on transient failures (429/5xx/transport errors).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import httpx

from mcp_servers_common.ratelimit import pace

from .parse import parse_article

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TOOL = "pubmed-fetch"
DEFAULT_BATCH_SIZE = 200
DEFAULT_SLEEP_S = 0.5
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}


class PubMedFetchError(RuntimeError):
    """Raised on unrecoverable E-utilities failures or missing PMIDs (strict mode)."""


@dataclass
class FetchStats:
    """Transport-level instrumentation for benchmarking."""

    http_requests: int = 0
    bytes_downloaded: int = 0
    bytes_decoded: int = 0
    request_log: list[dict] = field(default_factory=list)

    def reset(self) -> None:
        self.http_requests = 0
        self.bytes_downloaded = 0
        self.bytes_decoded = 0
        self.request_log = []


class PubMedFetcher:
    """Batched PubMed fetcher: epost the full PMID list, then efetch in batches."""

    def __init__(
        self,
        email: str,
        tool: str = DEFAULT_TOOL,
        api_key: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sleep_s: float = DEFAULT_SLEEP_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not email or "@" not in email:
            raise ValueError("NCBI etiquette requires a contact email address")
        if not (1 <= batch_size <= 200):
            raise ValueError("batch_size must be between 1 and 200 (efetch limit per request)")
        self.email = email
        self.tool = tool
        self.api_key = api_key
        self.batch_size = batch_size
        self.sleep_s = sleep_s
        self.max_retries = max_retries
        self.stats = FetchStats()
        self._client = httpx.Client(
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": f"{tool}/0.1.0 ({email})"},
        )



    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PubMedFetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()



    def _common_params(self) -> dict:
        params = {"tool": self.tool, "email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _request(self, endpoint: str, data: dict) -> httpx.Response:
        """POST to an E-utilities endpoint with politeness sleep + retry/backoff."""
        url = f"{EUTILS_BASE}/{endpoint}"
        payload = {**self._common_params(), **data}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.sleep_s:


                pace(url, self.sleep_s)
            t0 = time.perf_counter()
            try:
                resp = self._client.post(url, data=payload)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            elapsed = time.perf_counter() - t0
            self.stats.http_requests += 1
            self.stats.bytes_downloaded += resp.num_bytes_downloaded
            self.stats.bytes_decoded += len(resp.content)
            self.stats.request_log.append(
                {
                    "endpoint": endpoint,
                    "status": resp.status_code,
                    "wire_bytes": resp.num_bytes_downloaded,
                    "decoded_bytes": len(resp.content),
                    "seconds": round(elapsed, 3),
                }
            )
            if resp.status_code in RETRY_STATUS:
                last_exc = PubMedFetchError(
                    f"{endpoint} returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            resp.raise_for_status()
            return resp
        raise PubMedFetchError(
            f"{endpoint} failed after {self.max_retries + 1} attempts: {last_exc}"
        )

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(2.0 * (2**attempt), 30.0))



    def epost(self, pmids: Sequence[str]) -> tuple[str, str]:
        """POST the PMID list to the History server; return (webenv, query_key)."""
        resp = self._request("epost.fcgi", {"db": "pubmed", "id": ",".join(map(str, pmids))})
        root = ET.fromstring(resp.text)
        webenv = root.findtext("WebEnv")
        query_key = root.findtext("QueryKey")
        if not webenv or not query_key:
            raise PubMedFetchError(f"epost did not return WebEnv/QueryKey: {resp.text[:300]}")
        return webenv, query_key

    def efetch_batch(self, webenv: str, query_key: str, retstart: int, retmax: int) -> str:
        """Fetch one batch of full PubMed XML records from the History server."""
        resp = self._request(
            "efetch.fcgi",
            {
                "db": "pubmed",
                "WebEnv": webenv,
                "query_key": query_key,
                "retstart": retstart,
                "retmax": retmax,
                "retmode": "xml",
            },
        )
        return resp.text



    def fetch_articleset_xml(self, pmids: Sequence[str]) -> list[str]:
        """Return the raw PubmedArticleSet XML text of each efetch batch (1 per 200 PMIDs)."""
        pmids = [str(p) for p in pmids]
        if not pmids:
            return []
        webenv, query_key = self.epost(pmids)
        batches: list[str] = []
        for retstart in range(0, len(pmids), self.batch_size):
            batches.append(
                self.efetch_batch(webenv, query_key, retstart, self.batch_size)
            )
        return batches

    def fetch_xml(self, pmids: Sequence[str], strict: bool = True) -> dict[str, str]:
        """Fetch all PMIDs and return {pmid: per-PMID PubmedArticle XML string}.

        Output order follows the input PMID order (deterministic). With ``strict=True``
        (default) a missing PMID raises :class:`PubMedFetchError`; otherwise it is
        silently absent from the result.
        """
        pmids = [str(p) for p in pmids]
        elements = self._fetch_elements(pmids)
        out: dict[str, str] = {}
        for pmid in pmids:
            if pmid in elements:
                out[pmid] = ET.tostring(elements[pmid], encoding="unicode")
            elif strict:
                raise PubMedFetchError(f"PMID {pmid} missing from efetch response")
        return out

    def fetch_records(self, pmids: Sequence[str], strict: bool = True) -> list[dict]:
        """Fetch all PMIDs and return structured records (JSON-serializable dicts).

        Each record has: pmid, title, journal, year, doi, abstract, mesh_terms.
        Output order follows the input PMID order (deterministic).
        """
        pmids = [str(p) for p in pmids]
        elements = self._fetch_elements(pmids)
        records: list[dict] = []
        for pmid in pmids:
            if pmid in elements:
                records.append(parse_article(elements[pmid]))
            elif strict:
                raise PubMedFetchError(f"PMID {pmid} missing from efetch response")
        return records



    def _fetch_elements(self, pmids: Sequence[str]) -> dict[str, ET.Element]:
        """Fetch and split the batched response into per-PMID article elements."""
        elements: dict[str, ET.Element] = {}
        for batch_xml in self.fetch_articleset_xml(pmids):
            root = ET.fromstring(batch_xml)
            if root.tag != "PubmedArticleSet":
                raise PubMedFetchError(f"unexpected efetch root element: {root.tag}")
            for child in root:
                pmid_el = child.find(".//PMID")
                if pmid_el is None or not (pmid_el.text or "").strip():
                    continue
                elements[pmid_el.text.strip()] = child
        return elements


def chunked(seq: Sequence, size: int) -> Iterable[Sequence]:
    """Yield successive chunks of ``seq`` of length ``size``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
