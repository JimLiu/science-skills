"""Throttled, retrying HTTP client for NCBI E-utilities (shared-host etiquette)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds
from mcp_servers_common.ua import contact_email, product_ua

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "ncbi-elink"




EMAIL = os.environ.get("NCBI_EMAIL") or contact_email()
USER_AGENT = product_ua()

DEFAULT_MIN_INTERVAL_S = 0.5
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RETRIES = 4
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_BASE_S = 1.0


@dataclass
class RequestStats:
    """Counters for outbound HTTP traffic (used by bench scripts)."""
    requests: int = 0
    bytes_downloaded: int = 0

    def reset(self) -> None:
        self.requests = 0
        self.bytes_downloaded = 0


class EUtilsClient:
    """GET wrapper around E-utilities endpoints.

    - enforces a minimum interval between requests (default 0.5 s -> <= 2 req/s)
    - retries on 429/5xx (honouring Retry-After) and on connection/timeout errors,
      with exponential backoff
    - counts requests and downloaded bytes in .stats
    """

    def __init__(self,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 session: requests.Session | None = None) -> None:
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()


        self.session.headers["User-Agent"] = USER_AGENT
        self.stats = RequestStats()

    def _throttle(self) -> None:



        pace(EUTILS_BASE, self.min_interval_s)

    def get(self, endpoint: str, params: dict) -> requests.Response:
        """GET {EUTILS_BASE}/{endpoint} with throttling and retries.

        ``params`` values may be lists to produce repeated query parameters
        (e.g. {"id": ["7157", "672"]} -> id=7157&id=672), which is how elink
        keeps one linkset per input UID.
        """
        url = f"{EUTILS_BASE}/{endpoint}"
        merged = dict(params)
        merged.setdefault("tool", TOOL)
        if EMAIL:
            merged.setdefault("email", EMAIL)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=merged, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:



                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(BACKOFF_BASE_S * (2 ** attempt))
                    continue
                raise
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                retry_after = (resp.headers.get("Retry-After") or "").strip()
                delay = retry_after_seconds(retry_after, BACKOFF_BASE_S * (2 ** attempt))
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"retry loop exhausted: {last_exc}")
