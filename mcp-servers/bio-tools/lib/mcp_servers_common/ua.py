"""User-Agent and contact-email helpers for the bio-tools servers.

The servers run on the user's own machine, so the User-Agent names the software
and a contact, where one is sent, is the user's own. When the app starts a
server it sets ``OPERON_VERSION``, and ``OPERON_CONTACT_EMAIL`` only if the user
has agreed to share a contact email with research data services.
"""
from __future__ import annotations

import functools
import os
import re
from urllib.parse import urlsplit



_PRODUCT = "Claude-Science"
_PRODUCT_URL = "https://claude.com/product/claude-science"



_CONTACT_EMAIL_DOMAIN = "ncbi.nlm.nih.gov"




_HOSTS_THAT_KEEP_THE_CLIENTS_NAME = frozenset({"www.encodeproject.org"})


class ContactEmailRequired(RuntimeError):
    """Raised when an upstream's usage policy requires a contact email and
    none is available (no consent, no override). Tool wrappers catch this and
    return a structured ``{"error": "contact_email_required", ...}`` result."""


def contact_email() -> str | None:
    """User-consented contact email, or ``None`` when not provided."""
    return os.environ.get("OPERON_CONTACT_EMAIL") or None


def require_contact_email(*, env_override: str = "NCBI_EMAIL") -> str:
    """Return ``env_override`` > consented email; raise ``ContactEmailRequired``
    when neither yields a usable address. For upstreams (NCBI E-utilities)
    whose policy makes the contact mandatory rather than polite."""
    e = os.environ.get(env_override) or contact_email()
    if e and "@" in e:
        return e
    raise ContactEmailRequired(
        "This tool talks to NCBI E-utilities, which require a contact "
        "email per their usage policy. Enable 'Share contact email with "
        "research data services' in Settings → Privacy to provide one. "
        "If you've already enabled it, the connector picked up the setting "
        "before you approved — it will reconnect automatically; retry in a "
        "moment (or disable/re-enable the connector in Settings → Connectors). "
        f"(Advanced: if you run this server outside Claude Science, "
        f"set the {env_override} environment variable.)"
    )


class OpenAlexKeyRequired(RuntimeError):
    """Raised when a tool needs an OpenAlex API key and none is available.

    OpenAlex requires an api_key on ALL requests — there is no anonymous
    access and no mailto parameter; keyless production calls are
    unsupported.
    Tool wrappers catch this and return a structured
    ``{"error": "openalex_key_required", ...}`` result.
    """


def require_openalex_key() -> str:
    """Return the injected OpenAlex API key; raise ``OpenAlexKeyRequired``
    when absent. The CLI's connector provider injects ``OPENALEX_API_KEY``
    at spawn from the user's stored OpenAlex credential (and re-spawns this
    server when the credential changes)."""
    k = os.environ.get("OPENALEX_API_KEY") or None
    if k:
        return k
    raise OpenAlexKeyRequired(
        "OpenAlex requires a free API key for all requests (keyless calls "
        "are unsupported). Add one under Customize → Credentials → OpenAlex "
        "— create an OpenAlex account and copy the key from "
        "https://openalex.org/settings/api. After saving, this connector "
        "reconnects automatically; retry in a moment (or disable/re-enable "
        "it in Settings → Connectors). (Advanced: if you run this server "
        "outside Claude Science, set the OPENALEX_API_KEY environment "
        "variable.)"
    )


def openalex_key_usable(key: str) -> bool:
    """Whether `key` meets the app's own rule for an OpenAlex key it saves:
    8 to 512 characters of visible ASCII, which an ``Authorization`` header
    carries as they are."""
    return (8 <= len(key) <= 512 and key.isascii() and key.isprintable()
            and " " not in key)





OPENALEX_KEY_UNUSABLE = (
    "The saved OpenAlex API key can't be used as it is. Use 8 to 512 "
    "characters: the letters A to Z (capital or small), digits and keyboard "
    "punctuation marks, with no spaces or invisible characters. Copy it "
    "again from https://openalex.org/settings/api and save it under "
    "Customize → Credentials → OpenAlex."
)


def product_ua(*, include_email: bool = True) -> str:
    """``Claude-Science/{ver} (+{product URL})``, with ``; mailto:{address}``
    after the URL when the user consented and ``include_email`` is left on.
    Always ASCII. An address the header cannot carry (not ASCII, or holding a
    space, control character, parenthesis or backslash) is OMITTED, not
    rewritten: a rewritten one could identify an unrelated mailbox.
    """


    release = re.match(r"[0-9]+\.[0-9]+\.[0-9]+",
                       os.environ.get("OPERON_VERSION", ""))
    ver = release.group() if release else "dev"
    parts = [f"+{_PRODUCT_URL}"]
    email = contact_email() if include_email else None
    if email and email.isascii() and not re.search(r"[\x00-\x20\x7f()\\]", email):
        parts.append(f"mailto:{email}")
    return f"{_PRODUCT}/{ver} ({'; '.join(parts)})"


def install_user_agent() -> None:
    """Make requests and httpx send ``product_ua`` on every request and redirect
    hop, with the e-mail address only where that hop's URL allows it. A direct
    call to a host in ``_HOSTS_THAT_KEEP_THE_CLIENTS_NAME`` keeps its client's.

    It patches the layer below the clients because those are vendored: a
    re-vendor would bring each one's own name back.
    """
    import httpx
    import requests.adapters

    def named(send):
        @functools.wraps(send)
        def wrapper(self, request, *args, **kwargs):
            url = urlsplit(str(request.url))
            host = url.hostname or ""


            if (host not in _HOSTS_THAT_KEEP_THE_CLIENTS_NAME
                    or "mailto:" in request.headers.get("User-Agent", "")):
                request.headers["User-Agent"] = product_ua(include_email=(
                    url.scheme == "https"
                    and f".{host}".endswith(f".{_CONTACT_EMAIL_DOMAIN}")))
            return send(self, request, *args, **kwargs)
        return wrapper

    for cls, method in (
        (requests.adapters.HTTPAdapter, "send"),
        (httpx.HTTPTransport, "handle_request"),
        (httpx.AsyncHTTPTransport, "handle_async_request"),
    ):
        setattr(cls, method, named(vars(cls)[method]))
