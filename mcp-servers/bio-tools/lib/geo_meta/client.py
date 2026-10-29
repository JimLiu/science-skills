"""Polite, instrumented HTTP client for NCBI hosts.

Rules implemented here (wave-2 politeness contract):
- pacing: >= ``min_interval`` seconds between any two outbound requests to the
  same host, enforced PROCESS-wide via mcp_servers_common.ratelimit (default
  0.5 s -> <= 2 req/s shared across every NCBI client in the aggregate);
- gzip transfer encoding requested on every call;
- bounded retries with exponential backoff on 429 / 5xx / transport errors,
  honouring ``Retry-After`` when present;
- instrumentation: request count, wire (compressed) bytes downloaded, per-URL log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from mcp_servers_common.ratelimit import SHARED, HostPacer, retry_after_seconds
from mcp_servers_common.ua import product_ua





DEFAULT_USER_AGENT = product_ua()

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class RequestStats:
    """Cumulative transport statistics for one PoliteClient."""

    requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "bytes_downloaded": self.bytes_downloaded,
            "retries": self.retries,
        }


class PoliteClient:
    """Throttled httpx wrapper used for every outbound request made by geo_meta."""

    def __init__(
        self,
        min_interval: float = 0.5,
        max_retries: int = 4,
        timeout: float = 120.0,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: Optional[httpx.BaseTransport] = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleep



        if sleep is time.sleep and clock is time.monotonic:
            self._pacer = SHARED
        else:
            self._pacer = HostPacer(sleep=sleep, clock=clock)
        self.stats = RequestStats()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )


    def _record(self, response: httpx.Response, url: str) -> None:
        wire = getattr(response, "num_bytes_downloaded", 0) or 0
        if not wire:

            wire = len(response.content)
        self.stats.requests += 1
        self.stats.bytes_downloaded += int(wire)
        self.stats.log.append(
            {"url": url, "status": response.status_code, "wire_bytes": int(wire)}
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            self._pacer.pace(url, self.min_interval)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                self.stats.retries += 1
                self._sleep(min(2.0 ** attempt, 30.0))
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                attempt += 1
                self.stats.retries += 1
                retry_after = response.headers.get("Retry-After")
                delay = retry_after_seconds(retry_after, min(2.0 ** attempt, 30.0))
                self._record(response, url)
                self._sleep(delay)
                continue

            self._record(response, url)
            response.raise_for_status()
            return response


    def get(self, url: str, params: Optional[dict[str, Any]] = None) -> httpx.Response:
        return self._request("GET", url, params=params)

    def post(self, url: str, data: Optional[dict[str, Any]] = None) -> httpx.Response:
        return self._request("POST", url, data=data)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
