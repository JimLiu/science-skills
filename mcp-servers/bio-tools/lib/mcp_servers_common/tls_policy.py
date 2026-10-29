"""Outbound-TLS certificate policy for the bundled MCP servers — the
connectors' half of a daemon-gated relaxation.

Python 3.13 — what the shared bundled-MCP conda env provisions for fresh
installs — changed ``ssl.create_default_context()`` to OR in
``ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN`` (CPython
gh-107361); urllib3 2.x sets the same bits in its own context factory and
httpx builds its contexts from ``create_default_context()``, so every
connector in this env arms the flag whichever client it uses.
``VERIFY_X509_STRICT`` is OpenSSL's ``X509_V_FLAG_X509_STRICT``: it makes
verification additionally enforce RFC 5280 certificate-*profile*
conformance on every certificate in the chain — most visibly that a CA
certificate's Basic Constraints extension is marked critical.

Corporate TLS-inspection proxies (Zscaler, Netskope, ...) re-sign every
upstream with a customer-held root CA, and many of those roots are
non-conforming in exactly that way. Behind such a proxy a strict connector
fails with
``CERTIFICATE_VERIFY_FAILED: Basic Constraints of CA cert not marked
critical`` even though the chain is trusted, the signature is good, the
certificate is in date, and the hostname matches.

The relaxation is GATED, not unconditional: the strict default stands until
the daemon says otherwise. The daemon resolves the posture (an operator
override, then its corporate-TLS-inspection detection — the same signal its
own egress trust uses) and writes the answer into each connector's spawn
env as ``OPERON_MCP_X509_STRICT=0`` (relax) or ``=1`` (strict). This module
only reads that one variable; unset, empty, or anything else means strict,
so a server run standalone (development, tests) keeps Python's default.

When told to relax, ``apply_posture()`` wraps the two chokepoints every
client TLS socket passes through — ``SSLContext.wrap_socket``
(requests/urllib3, httpx's sync backend) and ``SSLContext.wrap_bio``
(httpx/anyio and asyncio) — and clears the ``X509_STRICT`` bit on the
context just before a *client* handshake. Server-side wraps are left
untouched, and so are chain-of-trust, signature, expiry and hostname
verification; ``VERIFY_X509_PARTIAL_CHAIN`` is deliberately kept. Patching at
the point of use, rather than at context construction, makes the policy
independent of which HTTP library a connector uses and of import order.

Installed (or not) by ``run_server.py`` — the single launch chokepoint for
all bundled Python connectors — before any connection is made. The
wrappers carry ``_x509_strict_relaxed = True``, so a repeat install, or any
out-of-band installer that already set the same marker on the chokepoints,
is recognised and the methods are never double-wrapped.
"""

from __future__ import annotations

import functools
import os
import ssl





POSTURE_ENV = "OPERON_MCP_X509_STRICT"







MARKER_ATTR = "_x509_strict_relaxed"


RELAXED = "relaxed"
STRICT = "strict"
NOT_ARMED = "not-armed"

_RELAX_VALUES = frozenset({"0", "relaxed", "relax", "false", "off", "no"})


def relax_requested() -> bool:
    """Did the daemon tell this connector to relax X509_STRICT?"""
    return os.environ.get(POSTURE_ENV, "").strip().lower() in _RELAX_VALUES


def _default_arms_strict(strict: int) -> bool:
    """Does this interpreter's default client context arm X509_STRICT?"""
    try:
        return bool(ssl.create_default_context().verify_flags & strict)
    except Exception:
        return True


def _client_side(args: tuple, kwargs: dict, server_side_pos: int) -> bool:
    """server_side arrives positionally or by name; relax client wraps only."""
    if "server_side" in kwargs:
        return not kwargs["server_side"]
    if len(args) > server_side_pos:
        return not args[server_side_pos]
    return True


def relax_x509_strict() -> str:
    """Install the wrap_socket/wrap_bio wrappers that clear X509_STRICT on
    client SSLContexts at connection time. Unconditional installer —
    :func:`apply_posture` is the gated entry point the launcher uses.

    Idempotent: returns :data:`RELAXED` once the wrappers are in place
    (this call or an earlier marker-setting install), :data:`NOT_ARMED` when
    this Python's defaults never set the flag (≤3.12, where CPython and
    urllib3 both gate it on 3.13+, or an ssl build without it) so there is
    nothing to relax.
    """
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if getattr(ssl.SSLContext.wrap_socket, MARKER_ATTR, False) and getattr(
        ssl.SSLContext.wrap_bio, MARKER_ATTR, False
    ):





        return RELAXED
    if not strict or not _default_arms_strict(strict):
        return NOT_ARMED

    orig_wrap_socket = ssl.SSLContext.wrap_socket
    orig_wrap_bio = ssl.SSLContext.wrap_bio

    def _relax(ctx: ssl.SSLContext) -> None:
        flags = ctx.verify_flags
        if flags & strict:
            ctx.verify_flags = flags & ~strict

    @functools.wraps(orig_wrap_socket)
    def wrap_socket(self: ssl.SSLContext, *args, **kwargs):

        if _client_side(args, kwargs, server_side_pos=1):
            _relax(self)
        return orig_wrap_socket(self, *args, **kwargs)

    @functools.wraps(orig_wrap_bio)
    def wrap_bio(self: ssl.SSLContext, *args, **kwargs):

        if _client_side(args, kwargs, server_side_pos=2):
            _relax(self)
        return orig_wrap_bio(self, *args, **kwargs)

    setattr(wrap_socket, MARKER_ATTR, True)
    setattr(wrap_bio, MARKER_ATTR, True)
    ssl.SSLContext.wrap_socket = wrap_socket
    ssl.SSLContext.wrap_bio = wrap_bio
    return RELAXED


def apply_posture() -> str:
    """The gated entry point: relax only when the daemon said so via
    :data:`POSTURE_ENV`; otherwise leave Python's strict default untouched."""
    if not relax_requested():
        return STRICT
    return relax_x509_strict()
