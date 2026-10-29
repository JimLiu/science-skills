"""Fail-closed serving gate for the standalone per-domain servers.

The aggregate (mcp_bio) applies deferred.json itself; when a domain server
runs STANDALONE (run_server.py mcp_<domain>) it must enforce the exact same
gate, or splitting the aggregate into per-domain connectors would silently
re-enable legally-deferred tools (KEGG/CADD/PanglaoDB license gate, plus any
domain/tool deferrals). Single source of truth stays mcp_bio/domains.json +
mcp_bio/deferred.json — read here as data files (no module import, so a
single domain server never pays for importing the whole 26-server fleet).

Contract (same as mcp_bio.load_deferred): unknown names in the gate fail
CLOSED with a RuntimeError; a server whose every tool is gated refuses to
start rather than serving an empty tool list.

No-bulk mode: when the daemon puts OPERON_MCP_NO_BULK in the spawn env, the
gate also removes deferred.json's no_bulk_tools and lets CALLS_PER_MINUTE
tool calls a minute through, per server process.
"""

from __future__ import annotations

import importlib.resources
import json
import math
import os
import threading
import time
from collections import deque

import anyio

from . import never_trust

CALLS_PER_MINUTE = 10
_clock = time.monotonic

_OVER_LIMIT = (
    "This build allows {n} calls a minute to this connector, counted across "
    "all sessions and sub-agents, and this call is over that. The limit is "
    "deliberate, not a fault, so please don't look for another way to reach "
    "the same data. Wait {wait} seconds and call again, or make do with fewer "
    "calls. If that stops you finishing, tell the user what you needed.")


def _load(resource: str) -> dict:
    with importlib.resources.files("mcp_bio").joinpath(resource).open("r") as f:
        return json.load(f)


def no_bulk() -> bool:
    """Did the daemon ask for no-bulk mode? Any non-empty value counts."""
    return bool(os.environ.get("OPERON_MCP_NO_BULK"))


def _call_limit():
    """Returns check(). In no-bulk mode check() raises when the call would be
    more than CALLS_PER_MINUTE in the last 60 seconds. Outside the mode it does
    nothing."""
    if not no_bulk():
        return lambda: None
    recent: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
    guard = threading.Lock()

    def check() -> None:
        with guard:
            now = _clock()
            if len(recent) == CALLS_PER_MINUTE and now - recent[0] < 60:
                raise RuntimeError(_OVER_LIMIT.format(
                    n=CALLS_PER_MINUTE,
                    wait=math.ceil(60 - (now - recent[0]))))
            recent.append(now)

    return check


def gated_tool_names() -> frozenset[str]:
    """Union of all tool names deferred.json removes from serving."""
    domains = _load("domains.json")
    gate = _load("deferred.json")
    all_tools = {t for roster in domains.values() for t in roster}
    bad_domains = set(gate.get("domains", [])) - set(domains)
    names = (set(gate.get("tools", []))
             | set(gate.get("license_tools", []))
             | set(gate.get("unavailable_tools", [])))
    if no_bulk():
        off = gate.get("no_bulk_tools")
        if not off:
            raise RuntimeError(
                "OPERON_MCP_NO_BULK is set but mcp_bio/deferred.json has no "
                "no_bulk_tools map, so this server will not start. Repackage "
                "with the map.")
        names |= {t for tools in off.values() for t in tools}
    bad_tools = names - all_tools
    if bad_domains or bad_tools:
        raise RuntimeError(
            "deferred.json names unknown to domains.json — failing closed: "
            f"domains={sorted(bad_domains)} tools={sorted(bad_tools)}")
    for d in gate.get("domains", []):
        names |= set(domains[d])
    return frozenset(names)


def apply_gate_fastmcp(fm) -> None:
    """Remove gated tools from a tier-2 FastMCP server before serving.

    Call from the server's main() (NOT at import time — the aggregate
    imports these modules and applies its own gate). Raises if the gate
    empties the server: a fully-deferred domain must refuse to start.

    Also ports the aggregate's CRITICAL dispatch safeguard:
    FastMCP calls SYNC tool functions inline on the event
    loop, and every tier-2 tool does blocking HTTP — one slow upstream
    would wedge the whole standalone server (listTools health probes,
    concurrent calls, cancellations) exactly like the pride_search
    stress-test incident the aggregate engineered around. call_tool is
    rebound to dispatch into a worker thread (own event loop — sync and
    async tools both work) behind ONE process-wide anyio.Lock: a
    standalone process serves a single domain, so the per-domain
    serialization the aggregate does per-package collapses to one lock
    (shared @lru_cache client wrapping a non-thread-safe
    requests.Session).
    """
    gated = gated_tool_names()
    served = list(fm._tool_manager._tools)
    for name in served:
        if name in gated:
            fm.remove_tool(name)
    if not fm._tool_manager._tools:
        raise RuntimeError(
            f"{fm.name}: every tool is deferred by mcp_bio/deferred.json — "
            "this domain is not cleared to serve standalone")
    if getattr(fm, "_operon_offloop_dispatch", False):
        return
    orig_call_tool = fm.call_tool
    lock = anyio.Lock()
    check = _call_limit()

    async def _call_tool_offloop(name, arguments):
        def _run_in_worker() -> object:
            return anyio.run(orig_call_tool, name, arguments)

        check()
        async with lock:
            with never_trust.surfaced():
                return await anyio.to_thread.run_sync(_run_in_worker)







    fm._mcp_server.call_tool(validate_input=False)(_call_tool_offloop)

    fm.call_tool = _call_tool_offloop
    fm._operon_offloop_dispatch = True


def apply_gate_tier1(t1) -> None:
    """Remove gated tools from a Tier1Server before serving.

    Serve-time only (call from the server's main()): build_server() must
    stay pristine — the drop-in parity tests and the aggregate consume the
    full schema set. Raises if the gate empties the server.

    Also serializes same-process dispatch:
    Tier1Server._call_tool offloads via anyio.to_thread with NO lock, while
    the domain's handlers share one @lru_cache client wrapping a
    requests.Session (not thread-safe, non-atomic stats writes). Each
    handler is wrapped with a process-wide
    threading.Lock, held inside the worker thread: same one-in-flight
    serialization the aggregate enforces per domain, while the event loop
    stays free.
    """
    gated = gated_tool_names()
    t1.schemas = [s for s in t1.schemas if s["name"] not in gated]
    t1.handlers = {k: v for k, v in t1.handlers.items() if k not in gated}
    if not t1.schemas:
        raise RuntimeError(
            f"{t1.name}: every tool is deferred by mcp_bio/deferred.json — "
            "this domain is not cleared to serve standalone")
    if getattr(t1, "_operon_serialized_dispatch", False):
        return
    lock = threading.Lock()
    check = _call_limit()

    def _serialized(handler):
        def run(args):
            check()
            with lock, never_trust.surfaced():
                return handler(args)
        return run

    t1.handlers = {k: _serialized(v) for k, v in t1.handlers.items()}
    t1._operon_serialized_dispatch = True
