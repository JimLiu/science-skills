"""Batched KEGG REST client.

KEGG REST (https://rest.kegg.jp) allows up to 10 entries per /get request.
The legacy Biopython pattern (Bio.KEGG.REST.kegg_get) issues one request per
entry; this client batches up to 10 entries per request, enforces a polite
minimum inter-request interval (default 0.35 s, i.e. < 3 req/s), never
parallelizes, and retries transient failures with exponential backoff.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence
from urllib.parse import quote

import requests

from .parse import parse_entry, split_flat

DEFAULT_BASE_URL = "https://rest.kegg.jp"
MAX_BATCH = 10

RETRYABLE_STATUS = (403, 429, 500, 502, 503, 504)


class KeggError(RuntimeError):
    """Raised when the KEGG REST API returns an unrecoverable error."""


@dataclass
class KeggEntry:
    """One retrieved KEGG entry."""

    requested_id: str
    entry_id: str
    raw: str
    record: dict


@dataclass
class TransportStats:
    http_requests: int = 0
    bytes_downloaded: int = 0


class KeggClient:
    """Polite, batched client for the KEGG REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = 0.35,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        timeout_s: float = 60.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", "kegg-fetch/0.1 (batched KEGG REST client)"
        )
        self.stats = TransportStats()
        self._last_request_start = 0.0


    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_start
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _request(self, path: str) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_start = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (2 ** attempt))
                    continue
                raise KeggError(f"KEGG REST request failed for {url}: {exc}") from exc
            self.stats.http_requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                time.sleep(self.backoff_s * (2 ** attempt))
                continue
            raise KeggError(f"KEGG REST error {resp.status_code} for {url}")
        raise KeggError(f"KEGG REST request failed for {url}: {last_exc}")


    def find(self, database: str, query: str) -> str:
        """Raw /find/<database>/<query> response (used only for battery freezing)."""
        db = quote(str(database), safe="")
        return self._request(f"find/{db}/{quote(str(query), safe='')}").text

    def get_raw(self, ids: Sequence[str]) -> str:
        """One batched /get call for up to MAX_BATCH entry ids; returns raw flat-file text."""
        ids = list(ids)
        if not ids:
            return ""
        if len(ids) > MAX_BATCH:
            raise ValueError(
                f"KEGG /get accepts at most {MAX_BATCH} entries per call, got {len(ids)}"
            )
        return self._request(
            "get/" + "+".join(quote(str(i), safe=":") for i in ids)).text

    def get_entries(self, ids: Sequence[str], batch_size: int = MAX_BATCH) -> List[KeggEntry]:
        """Fetch entries in batches of ``batch_size`` (<= 10), preserving input order.

        The batched response concatenates flat-file records separated by '///'
        terminator lines; it is split per entry and each chunk is mapped back to
        its requested id via the ENTRY token (KEGG echoes '7157' for 'hsa:7157'
        but the full id for pathways/compounds). Raises KeggError if KEGG returns
        no entry for any requested id, or an entry matching none of them.
        """
        if batch_size < 1 or batch_size > MAX_BATCH:
            raise ValueError(f"batch_size must be in 1..{MAX_BATCH}")
        ids = list(ids)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in request")
        found: Dict[str, KeggEntry] = {}
        for batch in chunk(ids, batch_size):
            text = self.get_raw(batch)
            for entry_text in split_flat(text):
                record = parse_entry(entry_text)
                requested = match_requested_id(record["entry_id"], batch)
                if requested is None:
                    raise KeggError(
                        f"KEGG returned entry '{record['entry_id']}' matching none of "
                        f"the requested ids {batch}"
                    )
                if requested in found:
                    raise KeggError(f"KEGG returned a duplicate record for '{requested}'")
                record["entry"] = requested
                found[requested] = KeggEntry(
                    requested_id=requested,
                    entry_id=record["entry_id"],
                    raw=entry_text,
                    record=record,
                )
        missing = [i for i in ids if i not in found]
        if missing:
            raise KeggError(f"KEGG returned no entry for: {', '.join(missing)}")
        return [found[i] for i in ids]


def chunk(items: Sequence[str], size: int) -> List[List[str]]:
    """Split a sequence into consecutive chunks of at most ``size`` items."""
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def match_requested_id(entry_token: str, requested_ids: Sequence[str]) -> Optional[str]:
    """Map the ENTRY token of a returned record back to the requested id.

    Gene entries echo only the accession part ('7157' for 'hsa:7157'); pathway
    and compound entries echo the full id ('hsa04110', 'C00031').
    """
    for rid in requested_ids:
        accession = rid.split(":", 1)[-1]
        if entry_token == rid or entry_token == accession:
            return rid
    return None
