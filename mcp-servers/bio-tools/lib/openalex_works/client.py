"""Throttled, retrying GET client for the OpenAlex REST API.

Authentication: OpenAlex requires an API key on EVERY request (since
2026-02-13; the anonymous polite pool and ``mailto`` are retired — this
client sends NO mailto, in the query or the User-Agent). This client sends
the key as ``Authorization: Bearer``, adds it to no URL, and resolves it
lazily per request from the constructor arg or the ``OPENALEX_API_KEY`` env
(injected at spawn by the CLI from the user's stored OpenAlex credential);
a missing key raises ``OpenAlexKeyRequired`` so the tool layer can return a
structured ``openalex_key_required`` result instead of a server crash.

Pacing/budget discipline (MCP transport allows < 60 s per tool call): one
client instance enforces a minimum interval between requests (default
0.5 s -> <= 2 req/s), request timeout 20 s, at most ONE retry on transient
5xx/transport errors (Retry-After honored, capped at 10 s). 401/403/409 (key
rejected / key required) and 429 (daily usage budget exhausted) are
terminal with actionable messages — retrying cannot help within a call.

Redirects: followed only on https://api.openalex.org, by the client itself
(``OpenAlexClient._send``).
"""
from __future__ import annotations

import json
import time
from urllib.parse import urljoin, urlsplit

import requests

from mcp_servers_common.ratelimit import retry_after_seconds
from mcp_servers_common.ua import (OPENALEX_KEY_UNUSABLE, OpenAlexKeyRequired,
                                   openalex_key_usable, product_ua,
                                   require_openalex_key)

BASE_URL = "https://api.openalex.org"




_OPENALEX_HTTPS_HOSTS = ("api.openalex.org", "api.openalex.org:443")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5
OPENALEX_REDIRECTED = (
    "OpenAlex answered this request with a redirect that was not followed. "
    "This says nothing about the API key: skip this look-up or try it "
    "again later."
)


USER_AGENT = product_ua(include_email=False)


def _scrub_key(text: str, key: str) -> str:
    """Redact the API key (raw + percent-encoded, incl. double-encoded)
    from any upstream/proxy text bound for a model-visible error — error
    pages can echo the request, headers included. Callers scrub a
    window much wider than the emitted snippet, THEN truncate —
    truncating first can cut the echoed key mid-way, leaving an
    unmatchable prefix (a 4096-char window vs a <=300-char snippet keeps
    any key that could reach the snippet fully inside the scrub)."""
    if not key or len(key) < 6:
        return text
    from urllib.parse import quote
    for form in (quote(quote(key, safe="")), quote(key, safe=""), key):
        text = text.replace(form, "[redacted]")
    return text


class OpenAlexApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(OpenAlexApiError):
    """The API returned 404 for the requested entity."""


class _BearerAuth(requests.auth.AuthBase):
    """Sets ``Authorization: Bearer <key>``. Passed as ``auth=`` so that a
    netrc entry for the host does not replace the header. requests sends no
    redirect hop for this client and reads no netrc for one."""

    def __init__(self, key: str):
        self._key = key

    def __call__(self, r):
        r.headers["Authorization"] = f"Bearer {self._key}"
        return r


class OpenAlexClient:
    """GET client returning parsed JSON bodies."""

    RETRY_STATUSES = {500, 502, 503, 504}

    def __init__(self, base_url: str = BASE_URL, api_key: str | None = None,
                 min_interval_s: float = 0.5, timeout_s: float = 20.0,
                 max_attempts: int = 2,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")



        self._api_key = api_key
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.session = session or requests.Session()







        self.session.get_redirect_target = lambda resp: None
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def _get_once(self, url: str, params: dict | None, auth: _BearerAuth):
        """One GET that follows nothing. However it ends, the pacing clock
        is stamped."""
        try:
            return self.session.get(url, params=params, auth=auth,
                                    timeout=self.timeout_s,
                                    allow_redirects=False)
        finally:
            self._last_request_t = time.monotonic()

    def _send(self, url: str, params: dict, auth: _BearerAuth):
        """One GET of ``url``, redirects followed here and not by requests.

        requests would decide each hop's credentials anew from netrc: a
        netrc login could go to an address the answering server chose, or
        take the key's place on a hop that stays. Here a redirect is followed
        only while it stays on https://api.openalex.org, with the same auth
        object and no query of ours, at most ``MAX_REDIRECTS`` times. Any
        other redirect raises ``OpenAlexApiError``; nothing is sent to where
        it points.
        """
        resp = self._get_once(url, params, auth)
        for _ in range(MAX_REDIRECTS):
            location = resp.headers.get("Location")
            if resp.status_code not in _REDIRECT_STATUSES or not location:
                return resp
            try:

                location = location.encode("latin1").decode("utf8")
                to = urlsplit(urljoin(url, location))
            except ValueError:
                to = None
            resp.close()
            if (to is None or to.scheme != "https"
                    or to.netloc.lower() not in _OPENALEX_HTTPS_HOSTS):
                raise OpenAlexApiError(OPENALEX_REDIRECTED)


            url = to.geturl()
            self._throttle()
            resp = self._get_once(url, None, auth)
        if (resp.status_code in _REDIRECT_STATUSES
                and resp.headers.get("Location")):
            resp.close()
            raise OpenAlexApiError(OPENALEX_REDIRECTED)
        return resp

    def _resolve_key(self) -> str:


        key = self._api_key or require_openalex_key()



        if not openalex_key_usable(key):
            raise OpenAlexApiError(OPENALEX_KEY_UNUSABLE)
        return key

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET ``base_url + path``; return the parsed JSON dict.

        Raises OpenAlexKeyRequired when no key is available, NotFound on
        404, OpenAlexApiError on other HTTP errors, an unsendable key, or an
        exhausted single retry on 5xx/transport failures. 401/403/409/429 are
        terminal with actionable messages (key rejected / key required /
        daily budget exhausted) — never retried, and the key value never
        appears in an error message (only status + a body snippet). A
        redirect that ``_send`` does not follow is terminal too and blames
        no key.
        """
        q = dict(params or {})
        key = self._resolve_key()
        auth = _BearerAuth(key)
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                resp = self._send(url, q, auth)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_attempts - 1:
                    time.sleep(2.0)
                continue
            self._last_request_t = time.monotonic()
            if resp.status_code == 404:
                raise NotFound(f"not found: {path}")
            if resp.status_code == 401:
                raise OpenAlexApiError(
                    "HTTP 401: OpenAlex rejected the API key. Re-check it "
                    "under Customize → Credentials → OpenAlex against "
                    "https://openalex.org/settings/api (keys are free "
                    "with an OpenAlex account)."
                )
            if resp.status_code == 403:




                raise OpenAlexApiError(
                    "HTTP 403: OpenAlex refused the request — either the "
                    "API key was rejected (re-check it under Customize → "
                    "Credentials → OpenAlex) or a query parameter is "
                    f"invalid. Upstream said: "
                    f"{_scrub_key(resp.text[:4096], key)[:200]}"
                )
            if resp.status_code == 409:



                raise OpenAlexApiError(
                    "HTTP 409: OpenAlex requires a valid API key for this "
                    "request. Re-check the key under Customize → "
                    "Credentials → OpenAlex (free at "
                    "https://openalex.org/settings/api)."
                )
            if resp.status_code == 429:
                raise OpenAlexApiError(
                    "HTTP 429: the OpenAlex API key is over its usage "
                    "limit — most commonly the daily budget is exhausted "
                    "(resets at 00:00 UTC; raise it at "
                    "https://openalex.org/settings/api). If this appeared "
                    "mid-burst, several processes may share the key — "
                    "retry after a pause instead of immediately."
                )
            if resp.status_code in self.RETRY_STATUSES:
                last_err = OpenAlexApiError(
                    f"HTTP {resp.status_code}: "
                    f"{_scrub_key(resp.text[:4096], key)[:200]}")


                delay = retry_after_seconds(
                    resp.headers.get("Retry-After", ""), 2.0, cap=10.0)
                if attempt < self.max_attempts - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise OpenAlexApiError(
                    f"HTTP {resp.status_code}: "
                    f"{_scrub_key(resp.text[:4096], key)[:300]}")
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                last_err = exc
                if attempt < self.max_attempts - 1:
                    time.sleep(2.0)
                continue



        detail = _scrub_key(repr(last_err), key)
        raise OpenAlexApiError(
            f"giving up after {self.max_attempts} attempts: {detail}")
