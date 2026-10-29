"""Error-payload detection for bio-tools MCP servers.

Several bio-tools handlers (variants NCBI tools, cellguide, tier-1
passthroughs) return ``{"error": "..."}`` dicts on the *success* path. The
MCP client renders these as green successes — a caller's ``try/except``
never fires and the agent mistakes an error for data (06-25 friction probe,
cross-cutting theme #1).

One owned predicate (``is_error_payload``) + one FastMCP decorator
(``raise_on_error_payload``) so the rule lives in exactly one place:

- **Tier-1** (``Tier1Server._call_tool``) sets ``isError=True`` on the
  ``CallToolResult`` when the handler's JSON text decodes to an error
  payload.
- **Tier-2 / FastMCP** tools that can return error dicts are decorated with
  ``@raise_on_error_payload`` (under ``@mcp.tool(...)``); the raise reaches
  FastMCP's ``Tool.run`` catch-all, which surfaces it as ``isError=True``
  with the message intact.
"""

from __future__ import annotations

import functools
import inspect

from mcp.server.fastmcp.exceptions import ToolError






_ERROR_PAYLOAD_MAX_KEYS = 3


def is_error_payload(result: object) -> bool:
    """True iff ``result`` is an error-shaped dict that should surface as
    ``isError=True`` instead of a green success."""
    return (isinstance(result, dict)



            and bool(result.get("error"))
            and len(result) <= _ERROR_PAYLOAD_MAX_KEYS)


def error_payload_message(result: dict) -> str:
    """Render an error-shaped dict as a single actionable message string
    (for the ``isError=True`` text block). Preserves ``error`` +
    ``message`` and names any extra keys (``suggestions`` etc.) so the
    caller knows there was structured detail."""
    parts = [str(result.get("error", "")).strip()]
    msg = result.get("message")
    if msg:
        parts.append(str(msg).strip())
    extras = {k: v for k, v in result.items()
              if k not in ("error", "message")}
    if extras:
        parts.append(f"(detail: {extras})")
    return ": ".join(p for p in parts if p) or "tool returned an error payload"


def raise_on_error_payload(fn):
    """FastMCP tool decorator: an ``{"error": ...}`` return raises
    ``ToolError`` instead of returning on the success path.

    Apply UNDER ``@mcp.tool(...)`` — ``functools.wraps`` preserves the
    signature FastMCP introspects for input/output schema generation (via
    ``inspect.signature``'s ``__wrapped__`` chase)."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            if is_error_payload(result):
                raise ToolError(error_payload_message(result))
            return result
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if is_error_payload(result):
            raise ToolError(error_payload_message(result))
        return result
    return wrapper
