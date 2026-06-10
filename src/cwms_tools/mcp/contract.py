"""Canonical agent-visible contract surface for the MCP server.

This module is the single source of truth for the capability fingerprint. It
introspects the *real* registered FastMCP tools — their input schemas, output
schemas, and annotations — rather than a names-only inventory, so a breaking
change to any tool's argument or result shape moves the fingerprint (the whole
point of `fingerprint_scope: "schema-contract"`).

Layering: `core/fingerprint.compute()` stays a pure hash over whatever dict it
is handed; the FastMCP-specific extraction lives here, in the MCP layer, so
`core/` never imports FastMCP. All three fingerprint call sites
(`capabilities_payload()`, the per-response `_source()`, and the CLI
`fingerprint` command) route through `canonical_fingerprint()` so they cannot
drift apart.

Tool schemas are static for a given code version, so `tool_definitions()` is
computed once and cached. The hash itself is recomputed on every
`canonical_fingerprint()` call because some fingerprint inputs (resolved API
root, installed package versions) are resolved at call time. If
`cli_contract_payload()` ever grows beyond dict assembly, add an `lru_cache`
parallel to `tool_definitions()`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypeVar

from cwms_tools.core import fingerprint
from cwms_tools.mcp.resources import RESOURCE_INVENTORY, TOOL_ERROR_CODES

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

_T = TypeVar("_T")


def _run_coro(factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Run an async factory to completion from sync code.

    Works whether or not an event loop is already running: with no loop we use
    `asyncio.run`; inside a running loop (e.g. a live tool handler that is the
    first caller) we run it on a dedicated thread with its own loop. Because
    `tool_definitions()` is cached, this bridge executes at most once per process.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(factory())).result()


@lru_cache(maxsize=1)
def tool_definitions() -> dict[str, dict[str, Any]]:
    """Return `{tool_name: {input_schema, output_schema, annotations}}`.

    Built by standing up the server once and reading each registered tool's
    MCP-level definition. Cached because the schema surface is static per
    process. `core.fingerprint.compute` consumes this dict directly.
    """
    # Lazy import breaks the import cycle: server -> tools -> contract -> server.
    from cwms_tools.mcp.server import build_server  # noqa: PLC0415

    async def _extract() -> dict[str, dict[str, Any]]:
        mcp = build_server()
        defs: dict[str, dict[str, Any]] = {}
        for tool in await mcp.list_tools():
            mcp_tool = tool.to_mcp_tool()
            annotations = mcp_tool.annotations
            defs[mcp_tool.name] = {
                "input_schema": mcp_tool.inputSchema,
                "output_schema": mcp_tool.outputSchema,
                "annotations": (
                    annotations.model_dump(mode="json", exclude_none=True)
                    if annotations is not None
                    else None
                ),
                # Per-tool error catalog is agent-visible surface, so it belongs
                # in the fingerprint: changing a tool's error codes is a contract change.
                "error_codes": TOOL_ERROR_CODES.get(mcp_tool.name, []),
            }
        return defs

    return _run_coro(_extract)


def canonical_fingerprint() -> str:
    """The one capability fingerprint shared by every agent-visible surface."""
    from cwms_tools.cli.commands.schema import cli_contract_payload  # noqa: PLC0415

    return fingerprint.compute(
        tools=tool_definitions(),
        resources=RESOURCE_INVENTORY,
        cli_contract=cli_contract_payload(),
    )


__all__ = ["canonical_fingerprint", "tool_definitions"]
