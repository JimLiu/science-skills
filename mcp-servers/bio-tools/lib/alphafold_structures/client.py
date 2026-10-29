"""HTTP client for the AlphaFold DB public API (alphafold.ebi.ac.uk).

Politeness: <= 2 requests/s (min 0.5 s between requests), identifying
User-Agent, at most ONE retry on 429/5xx/transport errors (per-tool budget:
well inside the 60 s MCP transport limit — timeout 20 s, short backoff).

Quirks verified live: unknown-but-valid accessions answer HTTP 404 with an
empty JSON object; malformed identifiers answer HTTP 400 with
``{"error": ...}``.
"""
from __future__ import annotations

import time
import urllib.parse

import httpx

from mcp_servers_common.ratelimit import retry_after_seconds

BASE_URL = "https://alphafold.ebi.ac.uk/api"
USER_AGENT = "alphafold-structures/0.1 (bio-tools fleet; python-httpx)"
MIN_INTERVAL_S = 0.5
RETRY_STATUS = {429, 500, 502, 503, 504}


class AlphaFoldError(RuntimeError):
    """Raised when the AlphaFold API returns an unrecoverable error."""


class InvalidAccessionError(AlphaFoldError):
    """Raised when the API rejects the identifier format (HTTP 400)."""


class NotFoundError(AlphaFoldError):
    """Raised when no prediction exists for the accession (HTTP 404)."""


class AlphaFoldClient:
    """Thin HTTP wrapper with throttling, bounded retries and accounting."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        max_retries: int = 1,
        min_interval_s: float = MIN_INTERVAL_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self._last_request_t = 0.0

        self.request_count = 0
        self.bytes_downloaded = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )


    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AlphaFoldClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def get_prediction(self, accession: str) -> list[dict]:
        """GET /prediction/{accession}; returns the raw model list.

        Raises NotFoundError (no prediction), InvalidAccessionError (bad
        format) or AlphaFoldError (transport/5xx after retries).
        """
        url = f"{self.base_url}/prediction/{urllib.parse.quote(accession, safe='')}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url)
                self._last_request_t = time.monotonic()
                self.request_count += 1
                self.bytes_downloaded += len(resp.content)
            except httpx.TransportError as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(2.0)
                    continue
                raise AlphaFoldError(f"transport error for {url}: {exc}") from exc

            if resp.status_code == 200:
                body = resp.json()
                if not isinstance(body, list):
                    raise AlphaFoldError(f"unexpected response shape for {url}")
                return body
            if resp.status_code == 404:
                raise NotFoundError(f"no prediction for {accession}")
            if resp.status_code == 400:
                raise InvalidAccessionError(resp.text[:300])
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = retry_after_seconds(retry_after, 2.0, cap=10.0)
                time.sleep(wait)
                last_err = AlphaFoldError(f"HTTP {resp.status_code} for {url}")
                continue
            raise AlphaFoldError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
        raise AlphaFoldError(f"retries exhausted for {url}: {last_err}")
