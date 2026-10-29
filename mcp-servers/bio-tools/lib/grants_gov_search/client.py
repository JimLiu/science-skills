"""Client for the Grants.gov search2 API (POST-only).

Endpoint: https://api.grants.gov/v1/api/search2
  * GET returns 403 — every call is a POST with a JSON body.
  * Response envelope: {"errorcode": 0, "msg": "...", "data": {...}};
    hits nest under data.oppHits, total under data.hitCount.
  * Facet blocks (oppStatusOptions, agencies, eligibilities,
    fundingCategories, fundingInstruments) ignore the filter on their own
    dimension, so they provide independent count formulations.
  * Default page size when ``rows`` is omitted: 25 (measured 2026-06-08).
  * Politeness: >= 0.5 s between requests (no documented higher limit).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from .spec import ALL_STATUSES, GrantsSearchSpec

API_URL = "https://api.grants.gov/v1/api/search2"
USER_AGENT = "bio-tools/grants-gov-search (research; github.com/anthropic-experimental/bio-tools)"


class GrantsGovError(RuntimeError):
    """API returned an HTTP error or a non-zero errorcode."""


class IncompleteRetrievalError(GrantsGovError):
    """Pagination walk did not reproduce hitCount (corpus moved mid-walk,
    duplicate/missing rows, or a silently-ignored invalid sortBy)."""


@dataclass
class SearchResult:
    records: list[dict]
    hit_count: int
    facets: dict
    requests_made: int
    bytes_downloaded: int

    @property
    def count(self) -> int:
        return len(self.records)


class GrantsGovClient:
    def __init__(self, session: requests.Session | None = None, *,
                 throttle_s: float = 0.5, rows_per_page: int = 1000,
                 timeout: float = 30.0, max_retries: int = 3):
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", USER_AGENT)
        self.throttle_s = throttle_s
        self.rows_per_page = rows_per_page
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request_t = 0.0
        self.total_requests = 0
        self.total_bytes = 0


    def _post(self, payload: dict) -> tuple[dict, int]:
        """One throttled POST; returns (data, n_bytes). Retries 5xx/connection
        errors with exponential backoff; non-zero errorcode raises."""
        last_exc = None
        for attempt in range(self.max_retries):
            wait = self.throttle_s - (time.monotonic() - self._last_request_t)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self._session.post(API_URL, json=payload, timeout=self.timeout)
                self._last_request_t = time.monotonic()
                self.total_requests += 1
                n_bytes = len(resp.content)
                self.total_bytes += n_bytes
                if resp.status_code >= 500:
                    raise GrantsGovError(f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    raise GrantsGovError(
                        f"HTTP {resp.status_code}: {resp.text[:200]} "
                        "(note: the endpoint is POST-only; GET returns 403)")
                body = resp.json()
                if body.get("errorcode") != 0:
                    raise GrantsGovError(
                        f"errorcode {body.get('errorcode')}: {body.get('msg')}")
                return body["data"], n_bytes
            except (requests.ConnectionError, requests.Timeout, GrantsGovError) as exc:
                last_exc = exc
                if isinstance(exc, GrantsGovError) and not str(exc).startswith("HTTP 5"):
                    raise
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise GrantsGovError(f"gave up after {self.max_retries} attempts: {last_exc}")


    def search(self, spec: GrantsSearchSpec) -> SearchResult:
        """Full retrieval: walk startRecordNum until every hit is fetched.

        Verifies completeness (len(records) == hitCount, ids unique).
        One automatic re-walk on mismatch (live corpus may move mid-walk);
        persistent mismatch raises IncompleteRetrievalError.
        """
        for walk_attempt in (1, 2):
            records: list[dict] = []
            facets: dict = {}
            n_req = 0
            n_bytes = 0
            start = 0
            hit_count = None
            while True:
                data, b = self._post(spec.to_payload(self.rows_per_page, start))
                n_req += 1
                n_bytes += b
                if hit_count is None:
                    hit_count = data["hitCount"]
                    facets = {k: data.get(k) for k in
                              ("oppStatusOptions", "agencies", "eligibilities",
                               "fundingCategories", "fundingInstruments",
                               "dateRangeOptions")}
                hits = data["oppHits"]
                records.extend(hits)
                start += len(hits)
                if start >= data["hitCount"] or not hits:
                    break
            ids = [r["id"] for r in records]
            if len(records) == hit_count and len(set(ids)) == len(records):
                return SearchResult(records, hit_count, facets, n_req, n_bytes)
        raise IncompleteRetrievalError(
            f"retrieved {len(records)} records ({len(set(ids))} unique ids) "
            f"vs hitCount {hit_count} after retry — check sortBy validity / corpus motion")

    def count(self, spec: GrantsSearchSpec) -> tuple[int, dict]:
        """hitCount + facet blocks without retrieving records (rows=0)."""
        data, _ = self._post(spec.to_payload(0, 0))
        facets = {k: data.get(k) for k in
                  ("oppStatusOptions", "agencies", "eligibilities",
                   "fundingCategories", "fundingInstruments")}
        return data["hitCount"], facets

    def lookup_opp_num(self, opp_num: str,
                       opp_statuses: str = ALL_STATUSES) -> list[dict]:
        """Exact opportunity-number lookup across (by default) all statuses."""
        spec = GrantsSearchSpec(opp_num=opp_num, opp_statuses=opp_statuses)
        return self.search(spec).records


    @staticmethod
    def status_facet_count(facets: dict, statuses: str) -> int:
        """Sum of oppStatusOptions counts over a pipe-delimited status set.
        The status facet ignores the oppStatuses filter, so this is an
        independent aggregation of the same query."""
        wanted = set(statuses.split("|"))
        by_value = {o["value"]: o["count"] for o in facets.get("oppStatusOptions") or []}
        return sum(by_value.get(s, 0) for s in wanted)

    @staticmethod
    def agency_facet_count(facets: dict, agency_code: str) -> int | None:
        """Count for one agency code, searching top-level and sub-agency facets."""
        for top in facets.get("agencies") or []:
            if top.get("value") == agency_code:
                return top.get("count")
            for sub in top.get("subAgencyOptions") or []:
                if sub.get("value") == agency_code:
                    return sub.get("count")
        return None

    @staticmethod
    def flat_facet_count(facets: dict, block: str, value: str) -> int | None:
        """Count for one value in a flat facet block
        (eligibilities / fundingCategories / fundingInstruments)."""
        for opt in facets.get(block) or []:
            if opt.get("value") == value:
                return opt.get("count")
        return None


    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._session.close()
        return False
