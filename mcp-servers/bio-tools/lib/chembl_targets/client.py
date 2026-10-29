"""HTTP client for the ChEMBL target / target_component / protein_classification routes.

Design points (see README):
  * polite to the shared EBI host: >= 0.5 s between requests (<= 2 req/s)
  * retries with exponential backoff on 429/5xx and connection errors,
    honouring Retry-After when present
  * every multi-record retrieval is fully paginated and verified against
    page_meta.total_count (PaginationError on mismatch)
  * deterministic output ordering (see records.py)
  * request_count / bytes_downloaded counters for benchmarking
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Iterable

import requests

from .records import build_target_record
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MIN_INTERVAL_S = 0.5
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RETRIES = 4
USER_AGENT = "chembl-targets/0.1.0 (bio-tools wave 3B)"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


_ITEMS_KEYS = {
    "target": "targets",
    "target_component": "target_components",
    "protein_classification": "protein_classifications",
}


class ChemblTargetsError(RuntimeError):
    """Base error for chembl-targets."""


class TargetNotFoundError(ChemblTargetsError):
    """A requested target ChEMBL ID (or entity) does not exist upstream."""


class PaginationError(ChemblTargetsError):
    """A paged retrieval did not return page_meta.total_count records."""


class ChemblTargetsClient:
    """Client for ChEMBL target-layer routes with verified pagination."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_s: float = 1.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        raw_sink: list[str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept", "application/json")
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.page_size = page_size
        self.timeout_s = timeout_s
        self.raw_sink = raw_sink
        self.request_count = 0
        self.bytes_downloaded = 0
        self._last_request_t = 0.0
        self._component_cache: dict[int, dict[str, Any]] = {}
        self._classification_cache: dict[int, dict[str, Any]] = {}



    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``base_url + path`` and return parsed JSON, with throttling and retries."""
        url = f"{self.base_url}{path}"
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_t = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_retries:
                    break
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (2**attempt))
                continue
            self.request_count += 1
            self.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                raise TargetNotFoundError(f"HTTP 404 for {resp.url}")
            if resp.status_code in RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}"
                if attempt == self.max_retries:
                    break
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, self.backoff_s * (2**attempt))
                if attempt < self.max_retries:
                    time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ChemblTargetsError(f"HTTP {resp.status_code} for {resp.url}: {resp.text[:300]}")
            if self.raw_sink is not None:
                self.raw_sink.append(resp.text)
            return resp.json()
        raise ChemblTargetsError(f"request failed after {self.max_retries + 1} attempts: {url} ({last_error})")

    def paginate(
        self,
        resource: str,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Retrieve every record of a filtered ``resource`` (limit/offset paging).

        Returns ``(records, total_count)``.  Unless ``max_records`` is given, the
        number of retrieved records is verified against page_meta.total_count and a
        PaginationError is raised on mismatch.
        """
        items_key = _ITEMS_KEYS.get(resource, f"{resource}s")
        query = dict(params or {})
        query["limit"] = page_size or self.page_size
        query["offset"] = 0
        records: list[dict[str, Any]] = []
        total: int | None = None
        while True:
            page = self.get_json(f"/{resource}.json", params=query)
            meta = page.get("page_meta") or {}
            total = meta.get("total_count")
            chunk = page.get(items_key) or []
            records.extend(chunk)
            if max_records is not None and len(records) >= max_records:
                return records[:max_records], total
            if not meta.get("next") or not chunk:
                break
            query["offset"] = int(query["offset"]) + len(chunk)
        if max_records is None and total is not None and len(records) != total:
            raise PaginationError(
                f"{resource}: retrieved {len(records)} records but page_meta.total_count={total}"
            )
        return records, total



    def classification(self, protein_class_id: int) -> dict[str, Any]:
        """Protein classification record (cached): {protein_class_id, pref_name, class_level, path}."""
        if protein_class_id not in self._classification_cache:
            raw = self.get_json(f"/protein_classification/{int(protein_class_id)}.json")
            self._classification_cache[protein_class_id] = {
                "protein_class_id": raw.get("protein_class_id"),
                "pref_name": raw.get("pref_name"),
                "class_level": raw.get("class_level"),
                "path": raw.get("protein_class_desc"),
            }
        return self._classification_cache[protein_class_id]

    def component(self, component_id: int) -> dict[str, Any]:
        """Raw target_component record (cached)."""
        if component_id not in self._component_cache:
            self._component_cache[component_id] = self.get_json(f"/target_component/{int(component_id)}.json")
        return self._component_cache[component_id]

    def component_classifications(self, component_id: int) -> list[dict[str, Any]]:
        """Resolved protein classification records for one component (sorted by id)."""
        raw = self.component(component_id)
        ids = sorted(
            pc.get("protein_classification_id")
            for pc in (raw.get("protein_classifications") or [])
            if pc.get("protein_classification_id") is not None
        )
        return [self.classification(i) for i in ids]

    def component_target_ids(self, accession: str) -> list[str]:
        """Target ChEMBL IDs containing ``accession``, via the target_component route.

        This is a *different query formulation* from targets_for_accession() and is
        used by the accuracy gate as independent ground truth.
        """
        components, _total = self.paginate("target_component", {"accession": accession})
        ids = {
            t.get("target_chembl_id")
            for comp in components
            for t in (comp.get("targets") or [])
            if t.get("target_chembl_id")
        }
        return sorted(ids)



    def fetch_target_raw(self, target_chembl_id: str) -> dict[str, Any]:
        """Raw single-target GET (no batching) -- used for spot checks."""
        return self.get_json(f"/target/{urllib.parse.quote(str(target_chembl_id), safe='')}.json")

    def fetch_targets(
        self,
        target_chembl_ids: Iterable[str],
        include_classification: bool = False,
        batch_size: int = 20,
    ) -> list[dict[str, Any]]:
        """Structured records for a list of target ChEMBL IDs (batched ``__in`` queries).

        Records are returned in input order.  Missing IDs raise TargetNotFoundError.
        """
        ids = list(target_chembl_ids)
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), batch_size):
            chunk = ids[start : start + batch_size]
            raw_items, _total = self.paginate(
                "target", {"target_chembl_id__in": ",".join(chunk)}
            )
            for raw in raw_items:
                found[raw["target_chembl_id"]] = raw
        missing = [i for i in ids if i not in found]
        if missing:
            raise TargetNotFoundError(f"target ChEMBL IDs not found: {missing}")
        return [self._build(found[i], include_classification) for i in ids]

    def _build(self, raw: dict[str, Any], include_classification: bool) -> dict[str, Any]:
        component_classifications = None
        if include_classification:
            component_classifications = {}
            for comp in raw.get("target_components") or []:
                cid = comp.get("component_id")
                component_classifications[cid] = (
                    self.component_classifications(cid) if cid is not None else []
                )
        return build_target_record(raw, component_classifications)



    def targets_for_accession(
        self, accession: str, include_classification: bool = False
    ) -> list[dict[str, Any]]:
        """All targets (single protein, complex, family, PPI, chimeric, ...) containing ``accession``.

        Fully paginated and verified against page_meta.total_count; sorted by target_chembl_id.
        """
        raw_items, _total = self.paginate(
            "target", {"target_components__accession": accession}
        )
        records = [self._build(raw, include_classification) for raw in raw_items]
        records.sort(key=lambda r: r["target_chembl_id"])
        return records

    def targets_for_accessions(
        self, accessions: Iterable[str], include_classification: bool = False
    ) -> dict[str, list[dict[str, Any]]]:
        """Reverse lookup for several accessions; dict preserves input order."""
        return {
            acc: self.targets_for_accession(acc, include_classification)
            for acc in accessions
        }

    def search_targets(
        self,
        filters: dict[str, Any],
        include_classification: bool = False,
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Declarative target search (any ChEMBL filter expressions), fully paginated.

        Returns ``(records sorted by target_chembl_id, page_meta.total_count)``.
        """
        raw_items, total = self.paginate(
            "target", dict(filters), page_size=page_size, max_records=max_records
        )
        records = [self._build(raw, include_classification) for raw in raw_items]
        records.sort(key=lambda r: r["target_chembl_id"])
        return records, total
