"""HTTP client for the ClinicalTrials.gov v2 API with complete pageToken pagination,
polite rate limiting, retries, and a provenance log of every request made."""

from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://clinicaltrials.gov/api/v2"


DEFAULT_FIELDS = "|".join([
    "NCTId", "BriefTitle", "OverallStatus", "Phase", "StudyType",
    "Condition", "InterventionType", "InterventionName",
    "EnrollmentCount", "EnrollmentType", "PrimaryCompletionDate",
    "LeadSponsorName", "LeadSponsorClass", "LocationCountry",
])

DEFAULT_PAGE_SIZE = 1000
RETRY_STATUS = {429, 500, 502, 503, 504}


class CTGovClient:
    """Thin synchronous client around /api/v2/studies.

    Parameters
    ----------
    page_size : page size used for paginated retrieval (API maximum is 1000).
    delay_s   : politeness delay inserted before every request after the first.
    max_retries : retries per request on 429/5xx, with exponential backoff.
    transport : optional httpx transport (e.g. httpx.MockTransport in offline tests).
    """

    def __init__(self,
                 base_url: str = BASE_URL,
                 page_size: int = DEFAULT_PAGE_SIZE,
                 delay_s: float = 0.6,
                 max_retries: int = 4,
                 timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = int(page_size)
        self.delay_s = float(delay_s)
        self.max_retries = int(max_retries)
        self.request_count = 0
        self.bytes_downloaded = 0
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "clinicaltrials-fetch/0.1 (anthropic-experimental/bio-tools)"},
        )



    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CTGovClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def get_json(self, path: str, params: dict[str, str]) -> tuple[dict, dict]:
        """GET one URL with retries; returns (parsed_json, call_meta)."""
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            if self.request_count > 0:
                time.sleep(self.delay_s)
            attempt += 1
            self.request_count += 1
            resp = self._client.get(url, params=params)
            self.bytes_downloaded += len(resp.content)
            if resp.status_code in RETRY_STATUS and attempt <= self.max_retries:
                time.sleep(min(2.0 ** attempt, 30.0))
                continue
            resp.raise_for_status()
            meta = {
                "url": str(resp.request.url),
                "status_code": resp.status_code,
                "bytes": len(resp.content),
                "attempts": attempt,
            }
            return resp.json(), meta



    def count(self, base_params: dict[str, str]) -> tuple[int, dict]:
        """Independent count-only request (countTotal=true, pageSize=1, fields=NCTId)."""
        params = dict(base_params)
        params.update({"countTotal": "true", "pageSize": "1", "fields": "NCTId"})
        data, meta = self.get_json("/studies", params)
        return int(data["totalCount"]), meta

    def paginate_studies(self,
                         base_params: dict[str, str],
                         fields: str = DEFAULT_FIELDS,
                         page_size: int | None = None) -> tuple[list[dict], int, list[dict]]:
        """Retrieve ALL studies matching base_params.

        Returns (studies, total_count, provenance) where provenance is one entry per
        HTTP request: url, params sent, page index, status, bytes, n studies in page,
        totalCount (first page only), and the pageToken used (None for page 0).
        """
        page_size = int(page_size or self.page_size)
        studies: list[dict] = []
        provenance: list[dict] = []
        total_count = -1
        page_token: str | None = None
        page_index = 0
        while True:
            params = dict(base_params)
            params["pageSize"] = str(page_size)
            params["fields"] = fields
            if page_index == 0:
                params["countTotal"] = "true"
            if page_token is not None:
                params["pageToken"] = page_token
            data, meta = self.get_json("/studies", params)
            page_studies = data.get("studies", [])
            studies.extend(page_studies)
            if page_index == 0:
                total_count = int(data.get("totalCount", -1))
            provenance.append({
                "page_index": page_index,
                "page_token": page_token,
                "url": meta["url"],
                "params": params,
                "status_code": meta["status_code"],
                "attempts": meta["attempts"],
                "bytes": meta["bytes"],
                "n_studies_in_page": len(page_studies),
                "total_count": total_count if page_index == 0 else None,
            })
            page_token = data.get("nextPageToken")
            page_index += 1
            if not page_token:
                break
        return studies, total_count, provenance
