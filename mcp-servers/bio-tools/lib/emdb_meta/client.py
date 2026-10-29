"""HTTP client for the EMDB REST API with rate limiting, retries and instrumentation."""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse

import requests

BASE_URL = "https://www.ebi.ac.uk/emdb/api"
USER_AGENT = "emdb-meta/0.1 (bio-tools; structured EMDB metadata retrieval)"


MIN_REQUEST_INTERVAL_S = 0.5


class EMDBNotFound(Exception):
    """Raised when an EMDB accession does not exist (HTTP 404)."""


class EMDBError(Exception):
    """Raised for unrecoverable API errors after retries."""


class EMDBClient:
    """Thin instrumented client for www.ebi.ac.uk/emdb/api.

    Counts requests and downloaded bytes; enforces a minimum interval between
    requests (default 0.5 s -> <= 2 req/s) and retries transient failures
    (HTTP 429/5xx, connection errors) with exponential backoff.
    """

    def __init__(self, base_url: str = BASE_URL, max_retries: int = 3,
                 min_interval_s: float = MIN_REQUEST_INTERVAL_S, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.request_count = 0
        self.bytes_downloaded = 0
        self._last_request_t = 0.0


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _get(self, url: str, accept: str) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                self._last_request_t = time.monotonic()
                resp = self.session.get(url, headers={"Accept": accept}, timeout=self.timeout_s)
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
                if resp.status_code == 404:
                    raise EMDBNotFound(url)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = EMDBError(f"HTTP {resp.status_code} for {url}")
                    if attempt < self.max_retries:
                        time.sleep(min(2.0 ** attempt, 8.0))
                    continue
                resp.raise_for_status()
                return resp
            except EMDBNotFound:
                raise
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(min(2.0 ** attempt, 8.0))
                continue
        raise EMDBError(f"request failed after {self.max_retries + 1} attempts: {url}") from last_exc


    def get_entry(self, emdb_id: str) -> dict:
        """GET /entry/{id} -> full entry JSON document (metadata only)."""
        emdb_id = normalize_emdb_id(emdb_id)
        resp = self._get(f"{self.base_url}/entry/{emdb_id}", accept="application/json")
        return resp.json()

    def search_page(self, query: str, *, rows: int = 200, page: int = 1,
                    fl: str | None = None, as_csv: bool = True):
        """GET /search/{query}?rows&page[&fl].

        With as_csv=True (and fl set) the API returns a compact CSV table; the
        default JSON form ignores fl and returns full entry documents.
        Returns a list of dicts (CSV rows) or the parsed JSON list.
        """
        q = urllib.parse.quote(query, safe="")
        params = {"rows": str(rows), "page": str(page)}
        if fl:
            params["fl"] = fl
        url = f"{self.base_url}/search/{q}?{urllib.parse.urlencode(params)}"
        if as_csv:
            resp = self._get(url, accept="text/csv")
            text = resp.text
            if not text.strip():
                return []
            reader = csv.DictReader(io.StringIO(text))
            return [dict(row) for row in reader]
        resp = self._get(url, accept="application/json")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise EMDBError(f"non-JSON search response for {url}") from exc

    def get_analysis(self, emdb_id: str) -> dict:
        """GET /analysis/{id} -> validation-analysis JSON (keyed by bare accession number).

        Source for the validation extension (Q-score, atom inclusion,
        recommended/predicted contour levels, FSC-derived resolution, volume
        estimates). Sparse for entries the validation pipeline has not fully
        processed (tomograms, historical entries).
        """
        emdb_id = normalize_emdb_id(emdb_id)
        resp = self._get(f"{self.base_url}/analysis/{emdb_id}", accept="application/json")
        return resp.json()

    def facet(self, query: str, field: str) -> dict:
        """GET /facet/{query}?field= -> {field: {value: count, ...}}."""
        q = urllib.parse.quote(query, safe="")
        url = f"{self.base_url}/facet/{q}?{urllib.parse.urlencode({'field': field})}"
        return self._get(url, accept="application/json").json()

    def yearly(self, query: str) -> dict:
        """GET /yearly/{query} -> {'annually': [{'year': y, 'value': n}, ...]}."""
        q = urllib.parse.quote(query, safe="")
        url = f"{self.base_url}/yearly/{q}"
        return self._get(url, accept="application/json").json()


def normalize_emdb_id(emdb_id: str) -> str:
    """Accept 'EMD-1234', 'emd-1234' or '1234' and return canonical 'EMD-1234'."""
    s = str(emdb_id).strip().upper()
    if s.startswith("EMD-"):
        s = s[4:]
    if not s.isdigit():
        raise ValueError(f"not a valid EMDB accession: {emdb_id!r}")
    return f"EMD-{s}"
