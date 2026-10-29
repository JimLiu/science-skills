"""CartBlanche22 (ZINC22) API client — the submit-poll transport.

CRITICAL CONTRACT: every CartBlanche22 search endpoint is ASYNC and
POST-only. A search is a form-encoded POST that returns a task receipt
``{"task": "<uuid>"}``; the result is fetched by polling
``GET /search/result/<uuid>`` until ``status`` is ``SUCCESS`` (or ``result``
is populated). A naive ``GET ?param=value`` returns HTTP 400 or the HTML
SPA shell — the documented drift trap this server exists to remove (the
zinc-database skill ablation showed callers improvising exactly that).
This client encapsulates the whole dance; tools present ONE synchronous
call and never expose the task id except in timeout errors (so a caller
could re-poll by hand).

Rate courtesy: ZINC publishes no hard limit; requests (submits AND polls)
are paced through the process-wide HostPacer at >= 1 s per request, and the
poll loop ticks at POLL_INTERVAL_S — never a tight loop.

Failure taxonomy (every arm raises ZincApiError with an actionable message,
never a stack trace): HTTP 400 (bad parameters), SPA-shell/non-JSON bodies
(request not understood as an API call), missing task receipt, server-side
FAILURE status, an unusable or blocked service (ZincUnavailable, ZincBlocked),
and poll deadline exhaustion (ZincTaskTimeout names the task uuid).
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote, urlsplit

import requests

from mcp_servers_common.ratelimit import pace, retry_after_seconds

BASE_URL = "https://cartblanche22.docking.org"
FILES_BASE_URL = "https://files.docking.org/zinc22"



DEFAULT_OUTPUT_FIELDS = "zinc_id,smiles,tranche_name,catalogs"

MIN_INTERVAL_S = 1.0
POLL_INTERVAL_S = 1.5





DEFAULT_TIMEOUT_S = 25.0
MIN_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 55.0



UNANSWERED_LIMIT_S = 10.0
SUBMIT_RETRIES = 2




MAX_RESPONSE_BYTES = 50 * 1024 * 1024


SOURCE_ORDER = ("zinc22", "zinc20")

_PENDING_STATUSES = frozenset({"PENDING", "STARTED", "PROGRESS", "RETRY"})
_TRANSIENT_CODES = frozenset({429, 502, 503, 504})


class ZincApiError(RuntimeError):
    """CartBlanche22 failure with a caller-actionable message."""


class ZincTaskTimeout(ZincApiError):
    """Poll deadline exhausted — names the task uuid for manual re-poll."""

    def __init__(self, task: str, waited_s: float,
                 last_transport_error: str | None = None) -> None:
        self.task = task



        if last_transport_error:
            cause = (f"the last poll attempt hit a transport error "
                     f"({last_transport_error}) — this may be a connectivity "
                     "problem, not server-side compute time")
        else:
            cause = "the server is likely still computing"
        super().__init__(
            f"ZINC task {task} did not complete within {waited_s:.0f}s — "
            f"{cause}. Re-poll {BASE_URL}/search/result/{task} later, or "
            "retry with a narrower query (smaller dist / fewer ids / lower "
            "count) or a larger timeout_s.")


class ZincUnavailable(ZincApiError):
    """The service sent no usable answer: silence, failed connections, or
    only retryable HTTP errors. `cause` names an exception class or
    "HTTP <code>"; it never carries text from the wire."""

    def __init__(self, cause: str) -> None:
        super().__init__(
            f"The ZINC service ({urlsplit(BASE_URL).netloc}) cannot be used "
            f"right now ({cause}). The search itself is very unlikely to be "
            "the reason, so don't call the ZINC tools again for now: tell "
            "the user that ZINC is down, busy or cannot be reached from this "
            "computer, then carry on without it (say so if you use another "
            "source) or try again later.")


class ZincBlocked(ZincApiError):
    """A proxy on the way answered 403 and opened no connection: a network
    policy's refusal of the host, which is no outage and no fault of ZINC's."""

    def __init__(self) -> None:
        super().__init__(
            "The network proxy refused to open a connection to "
            f"{urlsplit(BASE_URL).netloc} (ProxyError: the proxy answered "
            "403), so ZINC was never asked, and calling again will not help "
            "until that host is allowed. That is a network policy's block, "
            "not an outage: tell the user that they or their administrator "
            "would have to allow that host, or ask for access to it where "
            "that can be granted.")


def _looks_like_html(text: str) -> bool:
    """The CartBlanche22 SPA shell (returned for requests the API router
    does not recognize) instead of JSON."""
    head = text.lstrip()[:300].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def _body_excerpt(text: str, n: int = 200) -> str:
    excerpt = " ".join(text.split())[:n]
    return excerpt or "<empty body>"


class ZincClient:
    """Synchronous CartBlanche22 client hiding the async submit-poll API.

    ``session``/``sleep``/``clock``/``pace_fn`` are injectable for offline
    tests (same seam style as mcp_servers_common.ratelimit.HostPacer).
    """

    def __init__(self, session: requests.Session | None = None,
                 sleep=time.sleep, clock=time.monotonic, pace_fn=pace,
                 min_interval_s: float = MIN_INTERVAL_S,
                 poll_interval_s: float = POLL_INTERVAL_S) -> None:
        if session is None:
            session = requests.Session()
        self.session = session
        self._sleep = sleep
        self._clock = clock
        self._pace = pace_fn
        self.min_interval_s = min_interval_s
        self.poll_interval_s = poll_interval_s

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    def _http_timeout(self, deadline: float) -> tuple[float, float]:



        left = max(1.0, self._remaining(deadline))
        return left / 2, left



    def _submit(self, endpoint: str, data: dict, deadline: float) -> str:
        """POST form fields to a search endpoint; return the task uuid.

        Bounded by ``deadline``: each backoff and each request's timeouts
        are cut to the time left before it.
        """
        url = f"{BASE_URL}/{endpoint}"
        failure = "no attempt made"
        for attempt in range(SUBMIT_RETRIES + 1):
            if attempt > 0 and self._remaining(deadline) <= 0:
                break
            self._pace(url, self.min_interval_s)
            try:
                resp = self.session.post(url, data=data,
                                         timeout=self._http_timeout(deadline))
            except requests.RequestException as exc:



                if (isinstance(exc, requests.exceptions.ProxyError)
                        and "Tunnel connection failed: 403" in str(exc)):
                    raise ZincBlocked() from None

                failure = exc.__class__.__name__
                if (attempt < SUBMIT_RETRIES
                        and not isinstance(exc, requests.Timeout)):
                    self._sleep(min(2.0 * (attempt + 1),
                                    self._remaining(deadline)))
                    continue
                break

            if resp.status_code == 400:
                raise ZincApiError(
                    f"CartBlanche22 rejected the {endpoint} submission "
                    "(HTTP 400). The API accepts form-encoded POST fields "
                    "only; check the parameter values (SMILES syntax, "
                    "ZINC id / supplier-code format, subset name). Server "
                    f"detail: {_body_excerpt(resp.text)}")
            if resp.status_code in _TRANSIENT_CODES:
                failure = f"HTTP {resp.status_code}"
                if attempt < SUBMIT_RETRIES:
                    backoff = retry_after_seconds(
                        resp.headers.get("Retry-After"),
                        2.0 * (attempt + 1))
                    self._sleep(min(backoff, self._remaining(deadline)))
                    continue
                break
            if resp.status_code != 200:
                raise ZincApiError(
                    f"CartBlanche22 {endpoint} returned HTTP "
                    f"{resp.status_code}: {_body_excerpt(resp.text)}")

            if _looks_like_html(resp.text):
                raise ZincApiError(
                    f"CartBlanche22 returned its HTML app shell instead of "
                    f"a JSON task receipt for {endpoint} — the request was "
                    "not understood as an API call (this is what happens "
                    "for GET query-string requests or unknown form fields). "
                    f"Form fields sent: {sorted(data)}")
            try:
                payload = resp.json()
            except ValueError:
                raise ZincApiError(
                    f"CartBlanche22 {endpoint} returned a non-JSON body "
                    f"where a task receipt was expected: "
                    f"{_body_excerpt(resp.text)}")
            task = payload.get("task") if isinstance(payload, dict) else None
            if not task:
                raise ZincApiError(
                    f"CartBlanche22 {endpoint} response carried no task id "
                    f"(keys: {sorted(payload) if isinstance(payload, dict) else type(payload).__name__}) "
                    "— the async submit contract may have changed.")
            return str(task)
        raise ZincUnavailable(failure)



    def _read_capped(self, resp: requests.Response, task: str) -> str:
        """Streamed body read bounded at MAX_RESPONSE_BYTES.

        iter_content yields DECODED bytes, so the cap bounds what actually
        lands in memory — a runaway result fails loudly with a narrow-the-
        query message instead of building a multi-hundred-MB string in the
        one warm process (same shape as biomart_query's
        max_response_bytes guard).
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=1 << 20):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                resp.close()
                raise ZincApiError(
                    f"ZINC task {task} result exceeded "
                    f"{MAX_RESPONSE_BYTES} bytes — narrow the query "
                    "(smaller dist/adist, fewer ids, lower count) and "
                    "retry. The server-side task is complete; re-polling "
                    "will hit the same body.")
            chunks.append(chunk)
        return b"".join(chunks).decode(resp.encoding or "utf-8",
                                       errors="replace")

    def _poll(self, task: str, deadline: float, timeout_s: float) -> dict:
        """Poll /search/result/<task> until completion; return the payload."""


        url = f"{BASE_URL}/search/result/{quote(str(task), safe='')}"
        last_transport_error: str | None = None
        answered_at = self._clock()
        while True:
            self._pace(url, self.min_interval_s)
            pending = False
            miss = None







            try:
                resp = self.session.get(
                    url, stream=True, timeout=self._http_timeout(
                        min(deadline, answered_at + UNANSWERED_LIMIT_S)))
                last_transport_error = None
                if resp.status_code in _TRANSIENT_CODES:
                    resp.close()
                    miss = f"HTTP {resp.status_code}"
                    pending = True
                elif resp.status_code != 200:
                    body = self._read_capped(resp, task)
                    raise ZincApiError(
                        f"polling ZINC task {task} returned HTTP "
                        f"{resp.status_code}: {_body_excerpt(body)}")
                else:
                    body = self._read_capped(resp, task)
                    if _looks_like_html(body):
                        raise ZincApiError(
                            f"polling ZINC task {task} returned the HTML app "
                            "shell instead of JSON — the task id was not "
                            "recognized by /search/result/.")
                    try:
                        payload = json.loads(body)
                    except ValueError:
                        raise ZincApiError(
                            f"polling ZINC task {task} returned a non-JSON "
                            f"body: {_body_excerpt(body)}")
                    status = (payload.get("status")
                              if isinstance(payload, dict) else None)
                    if status == "FAILURE":
                        raise ZincApiError(
                            f"ZINC task {task} failed server-side (status "
                            "FAILURE). Check the query parameters (SMILES "
                            "syntax, id formats) and retry; if it persists "
                            "the upstream worker may be unhealthy.")








                    if isinstance(payload, dict) and (
                            status == "SUCCESS"
                            or (status is None and "result" in payload)):
                        return payload
                    if status in _PENDING_STATUSES or status is None:
                        pending = True
                    else:
                        raise ZincApiError(
                            f"ZINC task {task} reported unexpected status "
                            f"{status!r}.")
            except requests.RequestException as exc:



                miss = last_transport_error = exc.__class__.__name__
                pending = True
            if miss is None:
                answered_at = self._clock()
            elif self._clock() - answered_at >= UNANSWERED_LIMIT_S:
                raise ZincUnavailable(miss)
            if pending and self._clock() >= deadline:
                raise ZincTaskTimeout(task, timeout_s, last_transport_error)
            self._sleep(min(self.poll_interval_s, self._remaining(deadline)))



    def search(self, endpoint: str, data: dict,
               timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, list[dict]]:
        """Submit a search and block until its result is available.

        Returns the result as a dict keyed by source database (``zinc22``,
        ``zinc20``), each value a list of record dicts — the upstream shape,
        normalized so absent/odd payloads never leak to tools.

        ``timeout_s`` is the OVERALL submit→result budget:
        the deadline is computed once here and threaded
        through ``_submit`` (whose backoffs are clamped against it) and
        ``_poll`` so neither phase can run past it.
        """



        self.session.close()
        now = self._clock()
        deadline = now + timeout_s
        task = self._submit(endpoint, data,
                            min(deadline, now + UNANSWERED_LIMIT_S))
        payload = self._poll(task, deadline, timeout_s)
        result = payload.get("result")
        if result is None:
            return {}
        if isinstance(result, list):

            return {"zinc22": [r for r in result if isinstance(r, dict)]}
        if not isinstance(result, dict):
            raise ZincApiError(
                f"ZINC task {task} produced an unrecognized result shape "
                f"({type(result).__name__}) — expected a dict keyed by "
                "source database.")
        return {
            source: [r for r in records if isinstance(r, dict)]
            for source, records in result.items()
            if isinstance(records, list)
        }


def flatten_result(result: dict[str, list[dict]]) -> tuple[list[dict], dict[str, int]]:
    """Flatten a per-source result into one record list, each record tagged
    with its ``source``; zinc22 (current release) first. Returns the records
    and the per-source counts (the disclosure for capped responses)."""
    ordered = [s for s in SOURCE_ORDER if s in result]
    ordered += sorted(set(result) - set(SOURCE_ORDER))
    records: list[dict] = []
    counts: dict[str, int] = {}
    for source in ordered:
        rows = result[source]
        counts[source] = len(rows)
        records.extend({**row, "source": source} for row in rows)
    return records, counts
