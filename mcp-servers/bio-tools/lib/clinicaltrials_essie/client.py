"""HTTP client for the ClinicalTrials.gov v2 API: complete pageToken pagination,
polite rate limiting, retries with backoff, and a provenance log of every request.

requests-based (the sibling clinicaltrials-fetch/-results tools use httpx; this
wave's build environment standardizes on requests — behavior is identical).
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

import requests

BASE_URL = "https://clinicaltrials.gov/api/v2"


DEFAULT_FIELDS = "|".join([
    "NCTId", "BriefTitle", "OverallStatus", "Phase", "StudyType",
    "Condition", "LeadSponsorName", "LeadSponsorClass",
    "OverallOfficialName", "OverallOfficialAffiliation", "OverallOfficialRole",
    "ResponsiblePartyInvestigatorFullName",
    "MinimumAge", "MaximumAge", "Sex", "HealthyVolunteers",
    "LocationCity", "LocationState", "LocationCountry",
    "StudyFirstPostDate",
])

DEFAULT_PAGE_SIZE = 1000
RETRY_STATUS = {429, 500, 502, 503, 504}


class CTGovClient:
    """Thin synchronous client around /api/v2/studies.

    Parameters
    ----------
    page_size   : page size for paginated retrieval (API maximum is 1000).
    delay_s     : politeness delay before every request after the first.
    max_retries : retries per request on 429/5xx with exponential backoff.
    session     : optional requests.Session (inject a mocked session in tests).
    """

    def __init__(self,
                 base_url: str = BASE_URL,
                 page_size: int = DEFAULT_PAGE_SIZE,
                 delay_s: float = 0.6,
                 max_retries: int = 4,
                 timeout: float = 60.0,
                 session: requests.Session | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = int(page_size)
        self.delay_s = float(delay_s)
        self.max_retries = int(max_retries)
        self.timeout = float(timeout)
        self.request_count = 0
        self.bytes_downloaded = 0
        self._session = session or requests.Session()
        self._session.headers.update(
            {"User-Agent": "clinicaltrials-essie/0.1 (anthropic-experimental/bio-tools)"})



    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "CTGovClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def get_json(self, path: str, params: dict[str, str]) -> tuple[Any, dict]:
        """GET one URL with retries; returns (parsed_json, call_meta)."""
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            if self.request_count > 0:
                time.sleep(self.delay_s)
            attempt += 1
            self.request_count += 1
            resp = self._session.get(url, params=params, timeout=self.timeout)
            self.bytes_downloaded += len(resp.content)
            if resp.status_code in RETRY_STATUS and attempt <= self.max_retries:
                time.sleep(min(2.0 ** attempt, 30.0))
                continue
            resp.raise_for_status()
            meta = {
                "url": resp.url,
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
        """Retrieve ALL studies matching base_params via the pageToken walk.

        Returns (studies, total_count, provenance); provenance has one entry per
        HTTP request (url, page index, pageToken, status, bytes, page study count,
        totalCount on page 0).
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
            if fields:
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

    def get_study(self, nct_id: str, fields: str | None = None) -> tuple[dict, dict]:
        """GET /studies/{nctId} (single full record; used by the gate's reference capture)."""
        params: dict[str, str] = {}
        if fields:
            params["fields"] = fields
        return self.get_json(f"/studies/{urllib.parse.quote(str(nct_id), safe='')}", params)
