"""Tier-1 drop-in MCP server runner.

Tier-1 servers must expose tool names, parameter names, and parameter
schemas that match the ORIGINAL hosted connectors exactly. Rather than
fighting a schema generator into byte-parity, each tier-1 server embeds the
original connector's tool schemas verbatim (``schemas.json``, captured live
from the hosted connector — see ``mcp-servers/_snapshots/``) and serves them
through the low-level MCP ``Server`` API. Handlers receive the validated
arguments and return the tool's text output (a string, usually
pretty-printed JSON, matching the original connector's wire format).

Handlers are plain sync callables ``(args: dict) -> str`` — fleet packages
are synchronous (requests/httpx sync clients), so calls run in a worker
thread via ``anyio.to_thread``.
"""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Callable, Mapping

import anyio
import anyio.to_thread
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from .errors import is_error_payload



READ_ONLY = ToolAnnotations(readOnlyHint=True)



Handler = Callable[[dict], str]


def load_schemas(package: str, resource: str = "schemas.json") -> list[dict]:
    """Load the embedded verbatim tool schemas from a server package."""
    with importlib.resources.files(package).joinpath(resource).open("r") as f:
        data = json.load(f)
    return data["tools"] if isinstance(data, dict) else data


def original_json(obj: object, indent: int = 2) -> str:
    """Serialize like the original connectors do (pretty JSON, insertion
    order preserved, non-ASCII kept)."""
    return json.dumps(obj, indent=indent, ensure_ascii=False)


class Tier1Server:
    """A drop-in MCP server: verbatim schemas + per-tool handlers."""

    def __init__(self, name: str, schemas: list[dict],
                 handlers: Mapping[str, Handler]) -> None:
        schema_names = {s["name"] for s in schemas}
        missing = schema_names.symmetric_difference(handlers)
        if missing:
            raise ValueError(f"handler/schema mismatch: {sorted(missing)}")
        self.name = name
        self.schemas = schemas
        self.handlers = dict(handlers)
        self._output_schema = {s["name"]: s.get("output_schema") for s in schemas}
        self.server = Server(name)

        @self.server.list_tools()
        async def _list_tools() -> list[Tool]:
            return [
                Tool(name=s["name"], description=s["description"],
                     inputSchema=s["input_schema"],
                     outputSchema=s.get("output_schema"),
                     annotations=READ_ONLY)
                for s in self.schemas
            ]

        @self.server.call_tool()
        async def _call_tool(tool: str, arguments: dict | None):
            handler = self.handlers.get(tool)
            if handler is None:
                raise ValueError(f"Unknown tool: {tool}")
            args = dict(arguments or {})
            text = await anyio.to_thread.run_sync(lambda: handler(args))
            content = [TextContent(type="text", text=text)]






            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            structured = parsed if self._output_schema.get(tool) is not None \
                else None



            return CallToolResult(content=content, structuredContent=structured,
                                  isError=is_error_payload(parsed))

    def run(self) -> None:
        """Serve on stdio (blocking)."""

        async def _main() -> None:
            async with stdio_server() as (read, write):
                await self.server.run(
                    read, write, self.server.create_initialization_options()
                )

        anyio.run(_main)
