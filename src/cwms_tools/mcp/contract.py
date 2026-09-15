"""Canonical agent-visible contract surface for the MCP server.

This module is the single source of truth for the capability fingerprint. It
introspects the *real* registered FastMCP tools and resources — their
schemas, descriptions, and annotations — rather than a names-only inventory,
so a breaking change to any tool's argument/result shape OR a rewrite of its
selection-critical description text moves the fingerprint (the whole point of
`fingerprint_scope: "schema-contract"`, which despite the name has always
covered more than JSON schemas — see #71).

Layering: `core/fingerprint.compute()` stays a pure hash over whatever dict it
is handed; the FastMCP-specific extraction lives here, in the MCP layer, so
`core/` never imports FastMCP. All three fingerprint call sites
(`capabilities_payload()`, the per-response `_source()`, and the CLI
`fingerprint` command) route through `canonical_fingerprint()` so they cannot
drift apart.

Tool/resource schemas are static for a given code version, so
`tool_definitions()` and `resource_definitions()` are computed once each and
cached. The hash itself is recomputed on every `canonical_fingerprint()` call
because some fingerprint inputs (resolved API root, installed package
versions) are resolved at call time. If `cli_contract_payload()` ever grows
beyond dict assembly, add an `lru_cache` parallel to `tool_definitions()`.
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
    """Return `{tool_name: {title, description, input_schema, output_schema,
    annotations, error_codes}}`.

    Built by standing up the server once and reading each registered tool's
    MCP-level definition. Cached because the schema surface is static per
    process. `core.fingerprint.compute` consumes this dict directly.

    `description` is included (#71) because it is the primary agent-selection
    input — a rewrite that changes which tool an agent picks must move the
    fingerprint, not just a rewrite of the argument/result shape.
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
                "title": mcp_tool.title,
                "description": mcp_tool.description,
                "input_schema": mcp_tool.input_schema,
                "output_schema": mcp_tool.output_schema,
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


@lru_cache(maxsize=1)
def resource_definitions() -> dict[str, dict[str, Any]]:
    """Return `{uri_or_uri_template: {name, title, description, mime_type, error_codes}}`.

    Live-introspected (#71) the same way `tool_definitions()` is, so a resource
    description/title rewrite moves the fingerprint. `error_codes` has no
    FastMCP introspection point, so it's merged in from the hand-maintained
    `RESOURCE_INVENTORY`, matched by URI.
    """
    # Lazy import breaks the import cycle: server -> tools -> contract -> server.
    from cwms_tools.mcp.server import build_server  # noqa: PLC0415

    error_codes_by_uri = {
        entry["uri"]: entry.get("error_codes", []) for entry in RESOURCE_INVENTORY
    }

    async def _extract() -> dict[str, dict[str, Any]]:
        mcp = build_server()
        defs: dict[str, dict[str, Any]] = {}
        for resource in await mcp.list_resources():
            mcp_resource = resource.to_mcp_resource()
            uri = str(mcp_resource.uri)
            defs[uri] = {
                "name": mcp_resource.name,
                "title": mcp_resource.title,
                "description": mcp_resource.description,
                "mime_type": mcp_resource.mime_type,
                "error_codes": error_codes_by_uri.get(uri, []),
            }
        for template in await mcp.list_resource_templates():
            mcp_template = template.to_mcp_template()
            uri = mcp_template.uri_template
            defs[uri] = {
                "name": mcp_template.name,
                "title": mcp_template.title,
                "description": mcp_template.description,
                "mime_type": mcp_template.mime_type,
                "error_codes": error_codes_by_uri.get(uri, []),
            }
        return defs

    return _run_coro(_extract)


@lru_cache(maxsize=1)
def server_instructions() -> str:
    """The FastMCP server's `instructions` string (#71) — client-visible
    agent-selection guidance, so a rewrite must move the fingerprint.

    Read from the live built server (like `tool_definitions()`/
    `resource_definitions()`), not a module constant referenced in parallel —
    so a future `build_server()` change that alters what's actually registered
    can't drift from what gets fingerprinted.
    """
    # Lazy import breaks the import cycle: server -> tools -> contract -> server.
    from cwms_tools.mcp.server import build_server  # noqa: PLC0415

    return build_server().instructions or ""


def canonical_fingerprint() -> str:
    """The one capability fingerprint shared by every agent-visible surface."""
    from cwms_tools.cli.commands.schema import cli_contract_payload  # noqa: PLC0415
    from cwms_tools.mcp.fastmcp_capabilities import (  # noqa: PLC0415
        FALLBACKS,
        VERIFIED,
        VERIFIED_AGAINST,
    )
    from cwms_tools.mcp.resources import capability_contract_payload  # noqa: PLC0415

    resources = [{"uri": uri, **defn} for uri, defn in sorted(resource_definitions().items())]

    return fingerprint.compute(
        tools=tool_definitions(),
        resources=resources,
        cli_contract=cli_contract_payload(),
        runtime_baseline={
            "fastmcp_verified_against": VERIFIED_AGAINST,
            "verified": sorted(VERIFIED),
            "fallbacks": sorted(FALLBACKS),
        },
        capability_contract=capability_contract_payload(),
        server_instructions=server_instructions(),
    )


__all__ = [
    "canonical_fingerprint",
    "resource_definitions",
    "server_instructions",
    "tool_definitions",
]
