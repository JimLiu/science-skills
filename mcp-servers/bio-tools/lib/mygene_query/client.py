"""Direct client for the mygene.info v3 REST API using batched POST endpoints.

Endpoints used:
  POST /v3/query  -- querymany: up to 1000 query terms per request (scopes= controls which
                     identifier namespaces the terms are matched against)
  POST /v3/gene   -- getgenes:  up to 1000 Entrez/Ensembl gene IDs per request

Design points (vs. the legacy `mygene` client):
  * explicit fields= selection on every call (no implicit defaults)
  * deterministic output order: records are returned sorted by (input-term position, _id)
  * volatile metadata fields (_score, _version) are stripped from returned records
  * retries with exponential backoff on 429/5xx and transport errors
  * built-in instrumentation (n_requests, bytes_downloaded) for benchmarking
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable, Sequence, Union

import httpx

BASE_URL = "https://mygene.info/v3"
BATCH_SIZE = 1000
DEFAULT_TIMEOUT = 30.0
RETRY_STATUS = {429, 500, 502, 503, 504}
DROP_FIELDS = ("_score", "_version")

FieldSpec = Union[str, Sequence[str]]


def _as_field_str(fields: FieldSpec) -> str:
    """Normalize a field/scopes specification to the comma-separated form the API expects."""
    if isinstance(fields, str):
        return fields
    return ",".join(fields)


def _chunks(seq: Iterable, size: int):
    """Yield consecutive chunks of at most `size` items."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class MyGeneQueryClient:
    """Batched mygene.info v3 client with deterministic output and retries."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        min_request_interval: float = 0.34,
        batch_size: int = BATCH_SIZE,
        drop_fields: Sequence[str] = DROP_FIELDS,
        transport: httpx.BaseTransport | None = None,
        user_agent: str = "mygene-query/0.1.0 (bio-tools)",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.min_request_interval = min_request_interval
        self.batch_size = batch_size
        self.drop_fields = tuple(drop_fields)
        self.n_requests = 0
        self.bytes_downloaded = 0
        self._last_request_t = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            transport=transport,
        )


    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_request_interval:
            time.sleep(self.min_request_interval - dt)

    def _post(self, path: str, data: dict) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.post(url, data=data)
                self._last_request_t = time.monotonic()
                self.n_requests += 1
                self.bytes_downloaded += len(resp.content or b"")
                if resp.status_code in RETRY_STATUS:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code} from {url}",
                        request=resp.request,
                        response=resp,
                    )
                elif resp.status_code >= 400:
                    resp.raise_for_status()
                else:
                    return resp.json()
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                last_exc = exc
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff * (2**attempt))
        raise RuntimeError(
            f"POST {url} failed after {self.max_retries + 1} attempts"
        ) from last_exc


    def querymany(
        self,
        terms: Sequence[str],
        scopes: FieldSpec,
        fields: FieldSpec,
        species: str = "human",
    ) -> list[dict]:
        """Batch query terms against POST /v3/query (1000 terms per request).

        Note: terms are sent as a comma-separated list (same convention as the legacy
        client); terms containing commas are not supported.
        """
        terms = [str(t) for t in terms]
        records: list[dict] = []
        for chunk in _chunks(terms, self.batch_size):
            payload = {
                "q": ",".join(chunk),
                "scopes": _as_field_str(scopes),
                "fields": _as_field_str(fields),
                "species": species,
            }
            records.extend(self._post("/query", payload))
        return self._finalize(records, terms)

    def getgenes(
        self,
        ids: Sequence[str],
        fields: FieldSpec,
        species: str = "human",
    ) -> list[dict]:
        """Batch gene annotation fetch against POST /v3/gene (1000 ids per request)."""
        ids = [str(i) for i in ids]
        records: list[dict] = []
        for chunk in _chunks(ids, self.batch_size):
            payload = {
                "ids": ",".join(chunk),
                "fields": _as_field_str(fields),
                "species": species,
            }
            records.extend(self._post("/gene", payload))
        return self._finalize(records, ids)


    def _finalize(self, records: list[dict], input_terms: Sequence[str]) -> list[dict]:
        """Strip volatile fields and sort records deterministically.

        Sort key: (position of the record's `query` in the input list, _id, full record JSON).
        Records whose query is not in the input list (should not happen) sort last.
        """
        order = {t: i for i, t in enumerate(input_terms)}
        cleaned = []
        for rec in records:
            cleaned.append({k: v for k, v in rec.items() if k not in self.drop_fields})
        cleaned.sort(
            key=lambda r: (
                order.get(str(r.get("query")), len(order)),
                str(r.get("_id", "")),
                json.dumps(r, sort_keys=True, default=str),
            )
        )
        return cleaned

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MyGeneQueryClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
