"""HTTP client for the Sanger Cell Model Passports JSON:API.

Upstream: https://api.cellmodelpassports.sanger.ac.uk
(the bare host cellmodelpassports.sanger.ac.uk serves only the SPA frontend
and returns HTML for every /api path — always use the api. subdomain).

JSON:API conventions verified live 2026-06-08:
  * pagination:   page[size] / page[number] (1-based); meta.count = total rows.
    Default page size is 30. page[size]=100 is reliable; 500 times out server-side.
  * filtering:    flask-rest-jsonapi style ?filter=[{"name":..,"op":..,"val":..}]
    (JSON-encoded list). Nested relationship filters use op "has";
    to-many text matching uses op "any". `ilike` works on scalar columns
    (genes.symbol) but 500s on relationship-backed pseudo-columns (models.names).
  * includes:     ?include=sample.tissue,sample.cancer_type,model_msi_status
  * CRISPR KO dependencies: /genes/{SIDG}/datasets/crispr_ko
    (the unscoped /datasets/crispr_ko table has ~19.5M rows and relational
    filters on it time out — always go through the gene-scoped route).
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

import requests

API_BASE = "https://api.cellmodelpassports.sanger.ac.uk"
PAGE_SIZE = 100
MIN_REQUEST_INTERVAL = 0.5
TIMEOUT = 60

__version__ = "0.1.0"


class CMPError(RuntimeError):
    """Upstream returned a JSON:API error document or unusable payload."""


class CMPClient:
    """Thin, instrumented client. Counts requests and bytes for benchmarking."""

    def __init__(self, base_url: str = API_BASE, session: requests.Session | None = None,
                 min_interval: float = MIN_REQUEST_INTERVAL):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", f"bio-tools-depmap-models/{__version__}")
        self.min_interval = min_interval
        self.http_requests = 0
        self.bytes_downloaded = 0
        self._last_request_at = 0.0



    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(self.base_url + path, params=params, timeout=TIMEOUT)
        self._last_request_at = time.monotonic()
        self.http_requests += 1
        self.bytes_downloaded += len(resp.content)
        if resp.status_code == 404:
            raise KeyError(f"not found: {path}")
        resp.raise_for_status()
        doc = resp.json()
        if isinstance(doc, dict) and doc.get("errors"):
            raise CMPError(f"{path}: {doc['errors']}")
        return doc

    def _paged(self, path: str, params: dict[str, str] | None = None,
               page_size: int = PAGE_SIZE) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield (row, meta_count) over every page of a JSON:API collection."""
        page = 1
        while True:
            p = dict(params or {})
            p["page[size]"] = str(page_size)
            p["page[number]"] = str(page)
            doc = self._get(path, p)
            count = int(doc.get("meta", {}).get("count", 0))
            rows = doc.get("data") or []
            for row in rows:
                yield row, count
            if page * page_size >= count or not rows:
                return
            page += 1

    @staticmethod
    def _filter(spec: list[dict[str, Any]]) -> str:
        return json.dumps(spec, separators=(",", ":"))
