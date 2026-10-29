"""Minimal Open Targets Platform GraphQL client.

This is the one deliberate piece of new HTTP code in this server: no fleet
tool exposes arbitrary Open Targets GraphQL (``opentargets-assoc`` is
purpose-built for association scores only), so the passthrough lives here.

Known quirk this client exists to absorb: the platform API sometimes answers
a perfectly VALID query with HTTP 200 and
``{"errors": [{"message": "Internal server error"}]}``. Those are transient
and are retried with a short backoff. Deterministic GraphQL errors (unknown
field, bad argument — any message that is not an internal-server fault) are
NOT retried; they are returned honestly so the caller can fix the query.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://api.platform.opentargets.org/api/v4/graphql"
USER_AGENT = "mcp-clinical-genomics/0.1 (bio-tools; python-httpx)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


def _transient_graphql(errors: Any) -> bool:
    """True when every GraphQL error looks like a transient server fault."""
    if not isinstance(errors, list) or not errors:
        return False
    msgs = [str(e.get("message", "")).lower()
            for e in errors if isinstance(e, dict)]
    return bool(msgs) and all("internal server error" in m for m in msgs)


class OpenTargetsClient:
    """Paced (<=2 req/s), lightly retrying GraphQL POST client."""

    def __init__(self, base_url: str = BASE_URL, timeout_s: float = 60.0,
                 max_attempts: int = 3, backoff_s: float = 1.0,
                 min_interval_s: float = 0.5,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.min_interval_s = min_interval_s
        self._last_request_t = 0.0
        self._client = httpx.Client(
            timeout=timeout_s, transport=transport,
            headers={"User-Agent": USER_AGENT,
                     "Content-Type": "application/json"})

    def _pace(self) -> None:
        wait = self._last_request_t + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request_t = time.monotonic()

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """POST one GraphQL operation; return {data, errors?, attempts}."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        last_failure: dict | None = None
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                time.sleep(self.backoff_s * (attempt - 1))
            self._pace()
            try:
                resp = self._client.post(self.base_url, json=payload)
            except httpx.HTTPError as exc:
                last_failure = {"errors": [{"message": f"transport error: {exc}"}],
                                "attempts": attempt}
                continue
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.status_code in RETRY_STATUSES:
                last_failure = {"errors": [{"message": f"HTTP {resp.status_code}"}],
                                "http_status": resp.status_code,
                                "attempts": attempt}
                continue
            if not isinstance(body, dict):
                return {"errors": [{"message":
                                    f"non-JSON response (HTTP {resp.status_code})"}],
                        "http_status": resp.status_code, "attempts": attempt}
            errors = body.get("errors")
            if errors and _transient_graphql(errors) and attempt < self.max_attempts:
                last_failure = {"data": body.get("data"), "errors": errors,
                                "attempts": attempt}
                continue
            out: dict[str, Any] = {"data": body.get("data"), "attempts": attempt}
            if errors:
                out["errors"] = errors
            if resp.status_code != 200:
                out["http_status"] = resp.status_code
            return out
        assert last_failure is not None
        last_failure["attempts"] = self.max_attempts
        return last_failure
