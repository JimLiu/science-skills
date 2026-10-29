"""HTTP layer for api.fda.gov/drug/label.json.

- enforces a minimum interval between requests (anonymous limit: <= 240 req/min;
  default min_interval=0.4 s keeps us well under 3 req/s)
- retries 429 / 5xx / transport errors with exponential backoff
- pages through results with ``sort=set_id:asc`` so retrieval order is deterministic
- treats the openFDA 404 ``NOT_FOUND`` error as "zero results", not a failure
- counts outbound requests and wire (compressed) bytes for benchmarking
"""

from __future__ import annotations

import time

import httpx

BASE_URL = "https://api.fda.gov/drug/label.json"


MAX_PAGE_SIZE = 1000
MAX_SKIP = 25000

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenFDAError(RuntimeError):
    pass


class SkipCapExceeded(OpenFDAError):
    """Raised when a result set cannot be fully paged within the skip cap."""


class OpenFDAClient:
    def __init__(
        self,
        api_key: str | None = None,
        min_interval: float = 0.4,
        timeout: float = 60.0,
        max_retries: int = 5,
        page_size: int = MAX_PAGE_SIZE,
        base_url: str = BASE_URL,
    ):
        if not (1 <= page_size <= MAX_PAGE_SIZE):
            raise ValueError(f"page_size must be in [1, {MAX_PAGE_SIZE}]")
        self.api_key = api_key
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.page_size = page_size
        self.base_url = base_url
        self._client = httpx.Client(timeout=timeout)
        self._last_request_at = 0.0

        self.n_requests = 0
        self.bytes_downloaded = 0


    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _request(self, params: dict) -> dict:
        """One GET with throttling + retries.  Returns the parsed JSON body.

        A 404 whose body is the openFDA ``NOT_FOUND`` error is returned as-is
        (callers map it to an empty result set).
        """
        if self.api_key:
            params = {**params, "api_key": self.api_key}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.get(self.base_url, params=params)
                self._last_request_at = time.monotonic()
                self.n_requests += 1
                self.bytes_downloaded += resp.num_bytes_downloaded
            except httpx.TransportError as exc:
                self._last_request_at = time.monotonic()
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code in RETRYABLE_STATUS:
                last_exc = OpenFDAError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30))
                continue
            body = resp.json()
            if resp.status_code == 404 and body.get("error", {}).get("code") == "NOT_FOUND":
                return body
            if resp.status_code != 200:
                raise OpenFDAError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            return body
        raise OpenFDAError(f"request failed after {self.max_retries + 1} attempts: {last_exc}")


    def search_total(self, search: str) -> int:
        """The API's own matched-document count (``meta.results.total``)."""
        body = self._request({"search": search, "limit": 1})
        if body.get("error", {}).get("code") == "NOT_FOUND":
            return 0
        return body["meta"]["results"]["total"]

    def fetch_all(self, search: str, page_size: int | None = None) -> tuple[list[dict], int]:
        """Retrieve every label matching ``search``.

        Returns ``(raw_records, total)`` where ``total`` is ``meta.results.total``
        as reported by the API on the first page.  Records are retrieved with
        ``sort=set_id:asc`` and returned in that order.
        """
        size = page_size or self.page_size
        records: list[dict] = []
        skip = 0
        total: int | None = None
        while True:
            body = self._request(
                {"search": search, "limit": size, "skip": skip, "sort": "set_id:asc"}
            )
            if body.get("error", {}).get("code") == "NOT_FOUND":
                return records, (total if total is not None else 0)
            total = body["meta"]["results"]["total"]
            page = body.get("results", [])
            records.extend(page)
            skip += len(page)
            if skip >= total or not page:
                break
            if skip > MAX_SKIP:
                raise SkipCapExceeded(
                    f"result set of {total} records cannot be fully paged "
                    f"(skip cap {MAX_SKIP}); narrow the spec"
                )
        return records, total if total is not None else 0

    def fetch_by_set_id(self, set_id: str) -> dict | None:
        """Fetch the current label document for a single SPL set_id."""
        body = self._request({"search": f'set_id:"{set_id}"', "limit": 2})
        if body.get("error", {}).get("code") == "NOT_FOUND":
            return None
        results = body.get("results", [])
        return results[0] if results else None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenFDAClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
