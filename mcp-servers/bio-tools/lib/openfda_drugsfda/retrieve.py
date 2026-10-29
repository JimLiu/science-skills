"""Complete, verified retrieval against api.fda.gov/drug/drugsfda.json.

Encapsulated endpoint behavior (all measured live 2026-06-08):

- default page is ``limit=1`` — an unpaginated GET returns ONE application
  regardless of ``meta.results.total``;
- ``limit`` <= 1000 (limit=1001 -> HTTP 400), ``skip`` <= 25,000
  (skip=25,100 -> HTTP 400 "Skip value must 25000 or less."), so the deepest
  reachable row is 26,000 — larger result sets raise with guidance;
- zero matches -> HTTP 404 ``code: NOT_FOUND`` ("No matches found!"), mapped
  to an empty result, never an error;
- sorting: ``application_number`` is keyword-mapped and sortable RAW; the
  ``.exact`` suffix that sibling endpoints need does NOT exist here
  (``sort=application_number.exact:asc`` -> HTTP 500 query_shard_exception);
- counting: ``count=application_number`` and ``count=sponsor_name`` work raw
  (keyword fields, no ``.exact``); ``products.dosage_form.exact``,
  ``products.route.exact`` and ``openfda.pharm_class_*.exact`` need
  ``.exact``; ``sponsor_name.exact`` / ``products.marketing_status.exact``
  -> HTTP 404 "Nothing to count";
- count aggregations honor ``limit`` up to 1000 buckets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from .records import normalize_application
from .spec import SearchSpec
from mcp_servers_common.ratelimit import retry_after_seconds

BASE_URL = "https://api.fda.gov/drug/drugsfda.json"
PAGE_LIMIT = 1000
SKIP_CAP = 25_000
MAX_RETRIEVABLE = SKIP_CAP + PAGE_LIMIT
SORT = "application_number:asc"


COUNT_FIELDS = {
    "sponsor_name": "sponsor_name",
    "application_number": "application_number",
    "dosage_form": "products.dosage_form.exact",
    "route": "products.route.exact",
    "marketing_status": "products.marketing_status",
    "te_code": "products.te_code.exact",
    "pharm_class_epc": "openfda.pharm_class_epc.exact",
    "pharm_class_moa": "openfda.pharm_class_moa.exact",
    "pharm_class_cs": "openfda.pharm_class_cs.exact",
    "pharm_class_pe": "openfda.pharm_class_pe.exact",
}


class ResultSetTooLarge(RuntimeError):
    """Spec matches more rows than skip pagination can reach (26,000)."""


@dataclass
class RetrievalResult:
    total: int
    records: list[dict]
    last_updated: Optional[str] = None
    http_requests: int = 0


@dataclass
class CountResult:
    buckets: list[dict]
    bucket_sum: int = 0
    http_requests: int = 0

    def __post_init__(self) -> None:
        self.bucket_sum = sum(b["count"] for b in self.buckets)


class OpenFDADrugsFDAClient:
    """Session-scoped client with throttling, retries, and full pagination."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        min_interval_s: float = 0.45,
        max_retries: int = 4,
        timeout_s: float = 60.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "bio-tools/openfda-drugsfda")
        self._last_request_t = 0.0
        self.request_count = 0
        self.bytes_downloaded = 0



    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "OpenFDADrugsFDAClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, params: dict) -> Optional[dict]:
        """One throttled, retried GET. Returns None for 404 NOT_FOUND."""
        if self.api_key:
            params = {**params, "api_key": self.api_key}
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
            if wait > 0:
                time.sleep(wait)
            resp = self._session.get(BASE_URL, params=params, timeout=self.timeout_s)
            self._last_request_t = time.monotonic()
            self.request_count += 1
            self.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                body = resp.json()
                if body.get("error", {}).get("code") == "NOT_FOUND":
                    return None
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    delay = retry_after_seconds(retry_after, 2.0**attempt)
                    time.sleep(delay)
                    continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")



    def get_application(self, app_number: str) -> Optional[dict]:
        """Fetch one application by NDA/ANDA/BLA number; None if absent."""
        spec = SearchSpec(application_number=app_number)
        result = self.retrieve(spec)
        if not result.records:
            return None
        if len(result.records) > 1:
            raise RuntimeError(
                f"{app_number}: expected 1 record, got {len(result.records)}"
            )
        return result.records[0]

    def count(self, spec: SearchSpec) -> int:
        """Just the verified total for a spec (0 when nothing matches)."""
        body = self._get({"search": spec.to_search(), "limit": 1})
        return 0 if body is None else body["meta"]["results"]["total"]

    def retrieve(self, spec: SearchSpec) -> RetrievalResult:
        """Retrieve ALL applications matching the spec, count-verified."""
        search = spec.to_search()
        req0 = self.request_count
        records: list[dict] = []
        last_updated = None
        total = None
        skip = 0
        while True:
            if skip > SKIP_CAP:
                raise ResultSetTooLarge(
                    f"spec matches {total} applications; skip pagination reaches "
                    f"only {MAX_RETRIEVABLE}. Narrow the spec (e.g. add "
                    f"submission_date_from/to or extra filters)."
                )
            body = self._get(
                {"search": search, "sort": SORT, "limit": PAGE_LIMIT, "skip": skip}
            )
            if body is None:
                return RetrievalResult(
                    total=0, records=[], last_updated=None,
                    http_requests=self.request_count - req0,
                )
            total = body["meta"]["results"]["total"]
            last_updated = body["meta"].get("last_updated")
            if total > MAX_RETRIEVABLE:
                raise ResultSetTooLarge(
                    f"spec matches {total} applications (> {MAX_RETRIEVABLE} "
                    f"reachable via skip pagination). Narrow the spec."
                )
            page = body["results"]
            records.extend(normalize_application(r) for r in page)
            skip += PAGE_LIMIT
            if len(records) >= total or not page:
                break
        if len(records) != total:
            raise RuntimeError(
                f"retrieval incomplete: got {len(records)} of {total} "
                f"(search={search!r})"
            )
        return RetrievalResult(
            total=total,
            records=records,
            last_updated=last_updated,
            http_requests=self.request_count - req0,
        )

    def count_by(
        self, field_name: str, spec: Optional[SearchSpec] = None, limit: int = 1000
    ) -> CountResult:
        """count= aggregation; field_name is a COUNT_FIELDS key or raw field."""
        api_field = COUNT_FIELDS.get(field_name, field_name)
        params: dict = {"count": api_field, "limit": min(limit, 1000)}
        if spec is not None:
            params["search"] = spec.to_search()
        req0 = self.request_count
        body = self._get(params)
        buckets = [] if body is None else body["results"]
        return CountResult(buckets=buckets, http_requests=self.request_count - req0)

    def statistics(self) -> dict:
        """Corpus-level statistics (mirrors bc_get_drug_statistics)."""
        body = self._get({"limit": 1})
        assert body is not None
        total = body["meta"]["results"]["total"]
        by_status = self.count_by("marketing_status")
        by_form = self.count_by("dosage_form", limit=1000)
        by_route = self.count_by("route", limit=1000)
        by_sponsor = self.count_by("sponsor_name", limit=100)
        return {
            "total_applications": total,
            "last_updated": body["meta"].get("last_updated"),
            "marketing_status": by_status.buckets,
            "dosage_form_top": by_form.buckets[:25],
            "dosage_form_distinct": len(by_form.buckets),
            "route_top": by_route.buckets[:25],
            "route_distinct": len(by_route.buckets),
            "sponsor_top": by_sponsor.buckets[:25],
        }

    def pharmacologic_classes(self, class_type: str = "epc") -> CountResult:
        """Enumerate pharmacologic classes (epc|moa|cs|pe) with counts."""
        if class_type not in ("epc", "moa", "cs", "pe"):
            raise ValueError("class_type must be epc|moa|cs|pe")
        return self.count_by(f"pharm_class_{class_type}", limit=1000)

    def generic_equivalents(self, brand: str) -> dict:
        """Applications sharing the brand's active-ingredient set.

        Resolves the brand to its reference application(s), extracts the
        exact active-ingredient name set, then searches by each ingredient
        and keeps applications whose product ingredient set matches.
        """
        brand_result = self.retrieve(SearchSpec(brand=brand))
        if not brand_result.records:
            return {"brand": brand, "reference_applications": [], "equivalents": []}
        ingredient_sets = set()
        for rec in brand_result.records:
            for p in rec["products"]:
                names = frozenset(ai["name"] for ai in p["active_ingredients"])
                if names:
                    ingredient_sets.add(names)
        equivalents: dict[str, dict] = {}
        for names in ingredient_sets:
            spec = SearchSpec(active_ingredient=sorted(names)[0])
            try:
                cand = self.retrieve(spec)
            except ResultSetTooLarge:
                raise
            for rec in cand.records:
                rec_sets = {
                    frozenset(ai["name"] for ai in p["active_ingredients"])
                    for p in rec["products"]
                }
                if names in rec_sets:
                    equivalents[rec["application_number"]] = rec
        ref_apps = sorted({r["application_number"] for r in brand_result.records})
        return {
            "brand": brand,
            "reference_applications": ref_apps,
            "active_ingredient_sets": [sorted(s) for s in sorted(ingredient_sets, key=sorted)],
            "equivalents": sorted(equivalents.values(), key=lambda r: r["application_number"]),
        }
