"""Process-wide per-host request pacing.

The bio aggregate runs every domain server in ONE process, so per-instance
throttles multiply: five independent NCBI-facing clients each pacing at
<=2 req/s sum to ~10 req/s against NCBI's 3 req/s keyless cap.
This module is the shared gate: every client that talks to a shared upstream
host reserves its slot here, keyed by netloc, so the politeness interval is
enforced across the whole process instead of per client instance.

Usage (from a client's transport loop, in place of a local throttle):

    from mcp_servers_common.ratelimit import pace
    pace(url, self.min_interval_s)   # blocks until this process may send

Thread-safe: reservations are serialized under one lock; callers sleep
outside it, so a slow upstream never blocks other hosts' reservations.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit


class HostPacer:
    """Min-interval gate per netloc. One shared instance paces the process;
    tests may construct private instances with fake sleep/clock."""







    SHARED_BUDGET_SUFFIXES = ("ncbi.nlm.nih.gov",)

    def __init__(self, sleep=time.sleep, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._next_ok: dict[str, float] = {}
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def _key(cls, url_or_host: str) -> str:
        host = urlsplit(url_or_host).netloc or url_or_host
        for suffix in cls.SHARED_BUDGET_SUFFIXES:
            if host == suffix or host.endswith("." + suffix):
                return suffix
        return host

    def pace(self, url_or_host: str, min_interval_s: float) -> None:
        """Block until a request to this host is allowed; reserve the slot."""
        if min_interval_s <= 0:
            return
        host = self._key(url_or_host)
        with self._lock:
            now = self._clock()
            start = max(now, self._next_ok.get(host, 0.0))
            self._next_ok[host] = start + min_interval_s
        if start > now:
            self._sleep(start - now)


SHARED = HostPacer()


def pace(url_or_host: str, min_interval_s: float) -> None:
    """Reserve a request slot on the process-wide pacer (see HostPacer)."""
    SHARED.pace(url_or_host, min_interval_s)


def retry_after_seconds(header_value, fallback: float, cap: float = 60.0) -> float:
    """Parse a Retry-After header into a bounded sleep (RFC 7231 §7.1.3).

    Delay-seconds (integer OR fractional — ``"2.5"`` is honoured, where a
    bare ``isdigit()`` gate silently discarded it) are clamped to
    ``[0, cap]``; HTTP-dates, garbage, negatives, and NaN fall back to
    ``fallback`` (hostile ``Retry-After: -1``/``nan`` parse cleanly via
    float() but make ``time.sleep`` raise ValueError).
    THE fleet-wide predicate (promoted from uniprot_fetch): every
    hand-rolled retry loop
    routes its Retry-After through here — the census pin in
    test_operon_deltas rejects any raw ``float(retry_after)``/``isdigit``
    gate outside this module.
    """
    if not header_value:
        return fallback
    try:
        v = float(header_value)
    except ValueError:
        return fallback
    if v != v or v < 0:
        return fallback
    return min(v, cap)


try:
    from urllib3.util.retry import Retry as _Urllib3Retry

    class CappedRetry(_Urllib3Retry):
        """urllib3 Retry that honours server Retry-After only up to 60s.

        urllib3 honours Retry-After with NO ceiling when
        respect_retry_after_header=True (its default), so a single
        `Retry-After: 86400` pins the calling worker thread for a day —
        the same class of bug as the hand-rolled retry loops this module
        caps. Use this instead of Retry for any
        session-mounted adapter."""

        RETRY_AFTER_CAP_S = 60.0

        def get_retry_after(self, response):
            retry_after = super().get_retry_after(response)
            if retry_after is None:
                return None
            return min(retry_after, self.RETRY_AFTER_CAP_S)

except ImportError:
    CappedRetry = None