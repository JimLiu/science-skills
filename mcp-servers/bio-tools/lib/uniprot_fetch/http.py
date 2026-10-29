"""Instrumented HTTP session: request counting, wire-byte counting, retries, politeness throttle.

Both the legacy capture script and the modern client use this session so that the
benchmark instrumentation (request count, bytes downloaded) is measured identically
on both sides.
"""
from __future__ import annotations

import gzip
import io
import time
import zlib
from dataclasses import dataclass, field

import requests
from mcp_servers_common.ratelimit import retry_after_seconds


_MAX_GZIP_LAYERS = 3
_MAX_DECODED_BYTES = 64 * 1024 * 1024

DEFAULT_TIMEOUT = 120.0
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class HttpStats:
    requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    per_request: list = field(default_factory=list)

    def reset(self) -> None:
        self.requests = 0
        self.bytes_downloaded = 0
        self.retries = 0
        self.per_request = []


class InstrumentedSession:
    """Thin wrapper around ``requests.Session`` that

    - counts outbound HTTP requests and downloaded bytes as transferred
      (i.e. gzip-compressed sizes when the server applies Content-Encoding),
    - enforces a minimum interval between requests (politeness; EBI hosts are
      shared, stay <= 5 req/s per tool),
    - retries 429/5xx and connection errors with exponential backoff,
      honouring ``Retry-After``.
    """

    def __init__(
        self,
        min_interval_s: float = 0.21,
        max_tries: int = 5,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "uniprot-fetch/0.1 (bio-tools wave2; batched UniProt REST client)",
    ):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent




        self.min_interval_s = min_interval_s
        self.max_tries = max_tries
        self.timeout = timeout
        self.stats = HttpStats()
        self._last_request_t = 0.0



    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.min_interval_s - (now - self._last_request_t)
        if wait > 0:
            time.sleep(wait)
        self._last_request_t = time.monotonic()

    def get_bytes(self, url: str, params: dict | None = None) -> tuple[bytes, requests.Response]:
        """GET *url*; return ``(decoded_body_bytes, response)``.

        Wire bytes (compressed if the server compressed) are accumulated in ``self.stats``.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_tries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, stream=True, timeout=self.timeout)
                self.stats.requests += 1
                raw = resp.raw.read(decode_content=False)
                self.stats.bytes_downloaded += len(raw)
                self.stats.per_request.append(
                    {"url": resp.url, "status": resp.status_code, "wire_bytes": len(raw)}
                )
                if resp.status_code in RETRY_STATUSES:
                    self.stats.retries += 1
                    delay = retry_after_seconds(resp.headers.get("Retry-After"), 2.0 ** attempt)
                    if attempt < self.max_tries - 1:
                        time.sleep(delay)
                    continue
                resp.raise_for_status()
                body = _decode_body(raw, resp.headers.get("Content-Encoding", ""))
                return body, resp
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.stats.retries += 1
                if attempt < self.max_tries - 1:
                    time.sleep(2.0 ** attempt)
        raise RuntimeError(f"GET {url} failed after {self.max_tries} tries") from last_exc

    def get_text(self, url: str, params: dict | None = None) -> tuple[str, requests.Response]:
        body, resp = self.get_bytes(url, params=params)
        return body.decode("utf-8"), resp





def _bounded_gunzip(data: bytes) -> bytes:
    """Gunzip with the size cap enforced DURING expansion (1MB reads) — a
    bomb is rejected after at most cap+1MB is materialized, never after full
    expansion."""
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(1 << 20)
            if not chunk:
                return bytes(out)
            out += chunk
            if len(out) > _MAX_DECODED_BYTES:
                raise ValueError(
                    f"decompressed body exceeds {_MAX_DECODED_BYTES} bytes — "
                    "refusing to expand further (possible decompression bomb)")


def _bounded_inflate(data: bytes) -> bytes:
    """Deflate-decode with the same in-flight bound as _bounded_gunzip."""
    d = zlib.decompressobj()
    out = d.decompress(data, _MAX_DECODED_BYTES)
    if d.unconsumed_tail:
        raise ValueError(
            f"decompressed body exceeds {_MAX_DECODED_BYTES} bytes — "
            "refusing to expand further (possible decompression bomb)")
    return out + d.flush()


def _decode_body(raw: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").lower()
    body = raw


    if "gzip" in enc:
        body = _bounded_gunzip(body)
    elif "deflate" in enc:
        body = _bounded_inflate(body)






    for _ in range(_MAX_GZIP_LAYERS):
        if body[:2] != b"\x1f\x8b":
            break
        body = _bounded_gunzip(body)
    else:
        if body[:2] == b"\x1f\x8b":
            raise ValueError(
                f"body still gzip-wrapped after {_MAX_GZIP_LAYERS} layers — refusing "
                "to unwrap further (possible decompression bomb)")
    return body
