"""Direct ChEMBL REST client with batched paging and deterministic ordering."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from mcp_servers_common.ratelimit import CappedRetry
from requests.adapters import HTTPAdapter

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_TIMEOUT = 60.0
DEFAULT_POLITENESS_DELAY = 0.21
MAX_IDS_PER_IN_FILTER = 200


RESOURCES = {
    "molecule": ("molecules", "molecule_chembl_id"),
    "activity": ("activities", "activity_id"),
    "mechanism": ("mechanisms", "mec_id"),
}


@dataclass
class Instrumentation:
    """Counters for outbound HTTP traffic (single client instance)."""

    http_requests: int = 0
    bytes_downloaded: int = 0
    wall_clock_s: float = 0.0
    per_call: list = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "http_requests": self.http_requests,
            "bytes_downloaded": self.bytes_downloaded,
            "wall_clock_s": self.wall_clock_s,
        }


class ChEMBLClient:
    """Batched ChEMBL REST client.

    Parameters
    ----------
    base_url : ChEMBL data API root.
    page_size : records per page (ChEMBL caps this at 1000).
    timeout : per-request timeout in seconds.
    max_retries : transport-level retries on 429/5xx with exponential backoff.
    politeness_delay : sleep between successive page requests.
    session : optionally inject a pre-configured requests.Session (used by tests).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 5,
        backoff_factor: float = 1.0,
        politeness_delay: float = DEFAULT_POLITENESS_DELAY,
        session: requests.Session | None = None,
        user_agent: str = "chembl-bioactivity/0.1.0 (bio-tools)",
    ) -> None:
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be in 1..1000 (ChEMBL API cap)")
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.timeout = timeout
        self.politeness_delay = politeness_delay
        self.instrumentation = Instrumentation()

        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()


            retry = CappedRetry(
                total=max_retries,
                backoff_factor=backoff_factor,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET",),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": user_agent}
        )




    def _get(self, resource: str, params: dict) -> dict:
        """One GET against <base>/<resource>.json; updates instrumentation."""
        url = f"{self.base_url}/{resource}.json"
        t0 = time.perf_counter()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        elapsed = time.perf_counter() - t0
        resp.raise_for_status()
        n_bytes = len(resp.content)
        self.instrumentation.http_requests += 1
        self.instrumentation.bytes_downloaded += n_bytes
        self.instrumentation.wall_clock_s += elapsed
        self.instrumentation.per_call.append(
            {"url": url, "params": dict(params), "status": resp.status_code,
             "bytes": n_bytes, "seconds": round(elapsed, 4)}
        )
        return resp.json()

    def _paged(self, resource: str, params: dict, fields: list[str] | None = None) -> list[dict]:
        """Retrieve ALL records for a filter spec using limit=<page_size> offset paging."""
        if resource not in RESOURCES:
            raise ValueError(f"unknown resource {resource!r}; known: {sorted(RESOURCES)}")
        collection_key, sort_key = RESOURCES[resource]
        base_params = dict(params)
        base_params["limit"] = self.page_size

        base_params.setdefault("order_by", sort_key)
        if fields:
            base_params["only"] = ",".join(fields)

        records: list[dict] = []
        offset = 0
        total = None
        while True:
            page_params = dict(base_params, offset=offset)
            payload = self._get(resource, page_params)
            page = payload.get(collection_key, [])
            meta = payload.get("page_meta", {})
            total = meta.get("total_count", total)
            records.extend(page)
            if not page or meta.get("next") is None or (total is not None and len(records) >= total):
                break
            offset += self.page_size
            if self.politeness_delay:
                time.sleep(self.politeness_delay)
        if total is not None and len(records) != total:
            raise RuntimeError(
                f"{resource}: collected {len(records)} records but page_meta.total_count={total}"
            )

        records.sort(key=lambda r: (r.get(sort_key) is None, r.get(sort_key)))
        return records

    @staticmethod
    def _chunks(items: list[str], size: int) -> list[list[str]]:
        return [items[i : i + size] for i in range(0, len(items), size)]




    def get_molecules(self, chembl_ids: list[str], fields: list[str] | None = None) -> list[dict]:
        """Molecule records for an explicit list of ChEMBL IDs (batched __in filter)."""
        records: list[dict] = []
        for chunk in self._chunks(list(chembl_ids), MAX_IDS_PER_IN_FILTER):
            records.extend(
                self._paged("molecule", {"molecule_chembl_id__in": ",".join(chunk)}, fields=fields)
            )
        records.sort(key=lambda r: r.get("molecule_chembl_id") or "")
        return records

    def get_activities(
        self,
        target_chembl_id: str | None = None,
        standard_type: str | None = None,
        assay_type: str | None = None,
        pchembl_value_not_null: bool = False,
        extra_filters: dict | None = None,
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Bioactivity records matching the given filters (full pagination)."""
        params: dict = {}
        if target_chembl_id:
            params["target_chembl_id"] = target_chembl_id
        if standard_type:
            params["standard_type"] = standard_type
        if assay_type:
            params["assay_type"] = assay_type
        if pchembl_value_not_null:
            params["pchembl_value__isnull"] = "false"
        if extra_filters:
            params.update(extra_filters)
        if not params:
            raise ValueError("refusing to download the entire activity table: provide at least one filter")
        return self._paged("activity", params, fields=fields)

    def get_mechanisms(self, molecule_chembl_ids: list[str], fields: list[str] | None = None) -> list[dict]:
        """Mechanism-of-action records for an explicit list of molecule ChEMBL IDs."""
        records: list[dict] = []
        for chunk in self._chunks(list(molecule_chembl_ids), MAX_IDS_PER_IN_FILTER):
            records.extend(
                self._paged("mechanism", {"molecule_chembl_id__in": ",".join(chunk)}, fields=fields)
            )
        records.sort(key=lambda r: (r.get("mec_id") is None, r.get("mec_id")))
        return records
