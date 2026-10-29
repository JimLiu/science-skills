"""Throttled, retrying GET client for the UCSC Genome Browser REST API.

Politeness: one client instance enforces a minimum interval between
requests (default 0.5 s -> <= 2 req/s, UCSC asks programmatic users to
stay around 1 req/s bursts) and retries 429/5xx once with a short
backoff so a single tool call stays inside the MCP transport budget.
All endpoints are keyless GETs against https://api.genome.ucsc.edu.

Truncation honesty comes from the API itself: /getData/track responses
carry ``itemsReturned`` and set ``maxItemsLimit: true`` whenever the
``maxItemsOutput`` cap cut the listing short.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

BASE_URL = "https://api.genome.ucsc.edu"
USER_AGENT = "ucsc-tracks/0.1 (bio-tools fleet; python-requests)"


class UcscApiError(RuntimeError):
    """Unrecoverable API or transport error (carries the HTTP status)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0


class UcscClient:
    """GET client returning parsed JSON, plus thin endpoint wrappers."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, min_interval_s: float = 0.5,
                 timeout_s: float = 22.0, max_retries: int = 1,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0
        self._tracks_cache: dict[str, dict] = {}

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get(self, path: str, params: dict | None = None):
        """GET ``path`` (leading slash) and return the parsed JSON body.

        Raises UcscApiError with the upstream ``error`` message on non-200.
        """
        url = self.base_url + path
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params or {},
                                        timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(3.0)
                continue
            self._last_request_t = time.monotonic()
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code in self.RETRY_STATUSES and \
                    attempt < self.max_retries:
                last_err = UcscApiError(f"HTTP {resp.status_code}",
                                        resp.status_code)
                time.sleep(3.0)
                continue


            if resp.status_code not in (200, 206):
                try:
                    message = resp.json().get("error") or resp.text[:300]
                except json.JSONDecodeError:
                    message = resp.text[:300]
                raise UcscApiError(f"UCSC API {path}: HTTP "
                                   f"{resp.status_code}: {message}",
                                   resp.status_code)
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise UcscApiError(f"UCSC API {path}: non-JSON 200 body") \
                    from exc
        raise UcscApiError(f"UCSC API {path}: giving up after "
                           f"{self.max_retries + 1} attempts: {last_err!r}")


    def list_genomes(self) -> dict:
        """/list/ucscGenomes -> {genome_db: metadata dict}."""
        return self.get("/list/ucscGenomes").get("ucscGenomes", {})

    def list_tracks(self, genome: str, leaves_only: bool = True) -> dict:
        """/list/tracks -> {track_name: metadata dict}; cached per genome
        for the process lifetime (the hg38 leaf listing is ~17 MB — one
        download serves every subsequent filter)."""
        key = f"{genome}|{int(leaves_only)}"
        if key not in self._tracks_cache:
            payload = self.get("/list/tracks",
                               params={"genome": genome,
                                       "trackLeavesOnly": int(leaves_only)})
            tracks = payload.get(genome)
            if not isinstance(tracks, dict):
                raise UcscApiError(
                    f"UCSC API /list/tracks: no track dict for genome "
                    f"{genome!r} in response")
            self._tracks_cache[key] = tracks
        return self._tracks_cache[key]

    def chromosomes(self, genome: str) -> dict:
        """/list/chromosomes -> {chromCount, chromosomes: {name: size}}."""
        return self.get("/list/chromosomes", params={"genome": genome})

    def track_data(self, genome: str, track: str, chrom: str, start: int,
                   end: int, max_items: int | None = None) -> dict:
        """/getData/track -> full response dict (row list under the track
        name key, ``itemsReturned`` + optional ``maxItemsLimit`` flag)."""
        params: dict = {"genome": genome, "track": track, "chrom": chrom,
                        "start": start, "end": end}
        if max_items is not None:
            params["maxItemsOutput"] = max_items
        return self.get("/getData/track", params=params)
