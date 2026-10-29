"""HTTP client for the ENTRY-centric InterPro REST API routes.

This tool covers the entry-centric direction (search/detail/clans/members);
the sibling fleet tool ``interpro-domains`` covers the per-protein direction
(protein -> domain architecture). Do not duplicate that surface here.

Endpoints used
--------------
Entry keyword search (InterPro or any member DB, e.g. pfam)::

    GET /api/entry/{source_db}/?search=...&type=...&go_term=...&page_size=N
    Response: {"count": int, "next": url|null, "previous": url|null, "results": [...]}
    Cursor pagination: follow the "next" URL until null.

Entry detail (route chosen by accession prefix IPR -> interpro, PF -> pfam)::

    GET /api/entry/interpro/{IPRxxxxxx}/      GET /api/entry/pfam/{PFxxxxx}/

Pfam clans (sets)::

    GET /api/set/pfam/?search=...             (listing; HTTP 204 = no matches)
    GET /api/set/pfam/{CLxxxx}/               (detail; relationships.nodes = members)
    GET /api/entry/pfam/set/pfam/{CLxxxx}/    (independent member listing formulation)

Member proteins / proteomes of a Pfam family::

    GET /api/protein/{uniprot|reviewed|unreviewed}/entry/pfam/{PFxxxxx}/?tax_id=...
    GET /api/proteome/uniprot/entry/pfam/{PFxxxxx}/

Notes
-----
* HTTP 204 (No Content) is a legitimate empty result (e.g. a search with no
  hits) -- returned as count=0, results=[], never as an error.
* HTTP 404 means the accession is unknown.
* The ``?set=CLxxxx`` query parameter on /entry/pfam/ is silently IGNORED by
  the API (it returns the full unfiltered listing); the path-nested route
  /entry/pfam/set/pfam/{CLxxxx}/ is the correct filtered formulation.
* Retries: 408/429/5xx and transport errors retry with exponential backoff.
* Rate limiting: >=0.5 s between requests (<=2 req/s, the www.ebi.ac.uk
  politeness budget for this build wave).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests
from mcp_servers_common.pathseg import seg
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/interpro/api"
USER_AGENT = "interpro-entry-search/0.1 (bio-tools; entry-centric InterPro/Pfam retrieval)"

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

ENTRY_DB_BY_PREFIX = {"IPR": "interpro", "PF": "pfam"}


class InterProError(RuntimeError):
    """Raised for non-retryable API failures."""


class AccessionNotFound(InterProError):
    """Raised when the API returns 404 for an accession."""


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


class InterProEntryClient:
    """Synchronous client for the entry-centric InterPro REST API routes.

    Parameters
    ----------
    base_url:        API root (default: public EBI endpoint).
    page_size:       page size for paginated listings (default 200).
    min_interval_s:  minimum spacing between outbound requests (default 0.5 s).
    max_retries:     maximum retries per request for retryable failures.
    timeout_s:       per-request timeout.
    session:         optional requests.Session (offline tests inject a mock).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = 200,
        min_interval_s: float = 0.5,
        max_retries: int = 5,
        timeout_s: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.stats = RequestStats()
        self._last_request_t = 0.0
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Accept": "application/json", "User-Agent": USER_AGENT}
        )



    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "InterProEntryClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _get(self, url: str) -> requests.Response | None:
        """GET with throttling and retries. Returns None for HTTP 204."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(url, timeout=self.timeout_s)
                self._last_request_t = time.monotonic()
                self.stats.record(url, len(resp.content))
            except requests.RequestException as exc:
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
    def _backoff(attempt: int, resp: requests.Response | None = None) -> None:
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

    def _walk(self, url: str) -> dict:
        """Follow cursor pagination to completion; verify count == accumulated rows."""
        results: list = []
        count = 0
        pages = 0
        while url:
            payload = self._get_json(url)
            pages += 1
            if payload is None:
                if pages == 1:
                    return {"count": 0, "results": [], "pages": pages}



                raise InterProError(
                    f"upstream returned HTTP 204 mid-pagination (page {pages}) -- "
                    f"incomplete result set ({len(results)} of {count} rows); "
                    "this route's cursor pagination is defective upstream"
                )
            count = payload.get("count", 0)
            results.extend(payload.get("results", []))
            url = payload.get("next")
        if len(results) != count:
            raise InterProError(
                f"pagination incomplete: accumulated {len(results)} results "
                f"but API count is {count}"
            )
        return {"count": count, "results": results, "pages": pages}

    def _first_page(self, url: str) -> dict:
        """The naive baseline: one unpaginated request at the API default page size (20)."""
        resp = self._get(url)
        if resp is None:
            return {"count": 0, "results": [], "raw": ""}
        payload = resp.json()
        return {
            "count": payload.get("count", 0),
            "results": payload.get("results", []),
            "raw": resp.text,
        }

    @staticmethod
    def _qs(params: dict) -> str:
        parts = [f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v is not None]
        return ("?" + "&".join(parts)) if parts else ""



    def search_entries(
        self,
        q: str | None = None,
        entry_type: str | None = None,
        source_db: str = "interpro",
        go_term: str | None = None,
        page_size: int | None = None,
        first_page_only: bool = False,
    ) -> dict:
        """Keyword search over entries of one source DB, complete cursor walk.

        ``source_db`` is ``interpro`` or a member DB (``pfam``, ``smart``, ...).
        ``entry_type`` filters by entry type (domain, family, ...); ``go_term``
        by GO identifier (e.g. ``GO:0004672``).
        """
        params: dict = {"search": q, "type": entry_type, "go_term": go_term}
        base = f"{self.base_url}/entry/{seg(source_db)}/"
        if first_page_only:
            return self._first_page(base + self._qs(params))
        params["page_size"] = page_size or self.page_size
        return self._walk(base + self._qs(params))

    def get_entry(self, accession: str) -> dict:
        """Entry detail for an IPR or PF accession (route chosen by prefix)."""
        acc = accession.strip().upper()
        for prefix, db in ENTRY_DB_BY_PREFIX.items():
            if acc.startswith(prefix):
                payload = self._get_json(f"{self.base_url}/entry/{db}/{seg(acc)}/")
                if payload is None:
                    raise AccessionNotFound(f"empty response for entry {acc}")
                return payload
        raise ValueError(f"unrecognized entry accession (want IPR... or PF...): {accession}")

    def search_clans(self, q: str | None = None, page_size: int | None = None) -> dict:
        """Search Pfam clans (sets). Empty result (HTTP 204) -> count=0."""
        params = {"search": q, "page_size": page_size or self.page_size}
        return self._walk(f"{self.base_url}/set/pfam/" + self._qs(params))

    def get_clan(self, clan_acc: str) -> dict:
        """Clan (set) detail, including relationships.nodes (the member list)."""
        payload = self._get_json(
            f"{self.base_url}/set/pfam/{seg(clan_acc.strip().upper())}/")
        if payload is None:
            raise AccessionNotFound(f"empty response for clan {clan_acc}")
        return payload

    def clan_members(self, clan_acc: str, via: str = "set") -> dict:
        """Member families of a clan.

        ``via='set'`` reads relationships.nodes from the set detail (1 request);
        ``via='entry'`` walks /entry/pfam/set/pfam/{clan}/ (independent
        formulation, used by the accuracy gate as a cross-check).
        """
        acc = clan_acc.strip().upper()
        if via == "set":
            detail = self.get_clan(acc)
            nodes = detail.get("metadata", {}).get("relationships", {}).get("nodes", [])
            return {"count": len(nodes), "results": nodes, "pages": 1}
        if via == "entry":
            return self._walk(
                f"{self.base_url}/entry/pfam/set/pfam/{seg(acc)}/"
                + self._qs({"page_size": self.page_size})
            )
        raise ValueError(f"via must be 'set' or 'entry', got {via!r}")

    def entry_proteins(
        self,
        pf_acc: str,
        reviewed_only: bool = False,
        tax_id: str | int | None = None,
        count_only: bool = False,
        page_size: int | None = None,
        first_page_only: bool = False,
    ) -> dict:
        """Member proteins of a Pfam family (paged with count verification).

        ``count_only=True`` issues a single page_size=1 request and returns just
        the count -- use this for very large families (PF00069 has >1.5M
        proteins; walking that is neither polite nor useful).
        """
        db = "reviewed" if reviewed_only else "uniprot"
        base = (f"{self.base_url}/protein/{db}/entry/pfam/"
                f"{seg(pf_acc.strip().upper())}/")
        if first_page_only:
            return self._first_page(base + self._qs({"tax_id": tax_id}))
        if count_only:
            payload = self._get_json(base + self._qs({"tax_id": tax_id, "page_size": 1}))
            return {"count": (payload or {}).get("count", 0), "results": None, "pages": 1}
        return self._walk(
            base + self._qs({"tax_id": tax_id, "page_size": page_size or self.page_size})
        )

    def entry_proteomes(
        self,
        pf_acc: str,
        count_only: bool = False,
        page_size: int | None = None,
        first_page_only: bool = False,
    ) -> dict:
        """Proteomes containing members of a Pfam family.

        WARNING (verified 2026-06-08, release 108.0): the upstream
        ``/proteome/uniprot/entry/pfam/{acc}/`` route has DEFECTIVE cursor
        pagination -- the final partial page returns HTTP 204 instead of its
        rows, and row sets differ across page sizes (duplicates + omissions).
        A full walk therefore raises ``InterProError`` rather than silently
        returning a partial set. Use ``count_only=True`` (the ``count`` field
        is consistent and is what this tool's gate verifies); the sibling
        ``/protein/...`` route paginates correctly and is unaffected.
        """
        base = (f"{self.base_url}/proteome/uniprot/entry/pfam/"
                f"{seg(pf_acc.strip().upper())}/")
        if first_page_only:
            return self._first_page(base)
        if count_only:
            payload = self._get_json(base + self._qs({"page_size": 1}))
            return {"count": (payload or {}).get("count", 0), "results": None, "pages": 1}
        return self._walk(base + self._qs({"page_size": page_size or self.page_size}))

    def release_version(self) -> dict:
        """Current InterPro release version + date (/utils/release/current/)."""
        payload = self._get_json(f"{self.base_url}/utils/release/current/")
        return {
            "version": (payload or {}).get("version"),
            "release_date": (payload or {}).get("release_date"),
        }
