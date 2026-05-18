"""FastMCP 3 server construction. Tools register here; resources too.

Importing this module is side-effect-free — `build_server()` is the only
factory and it returns a freshly-configured FastMCP instance. The CLI
`mcp serve` subcommand imports this factory and runs it.

Task tools (place, value, region, publisher) register via the helpers
in `cwms_tools.mcp.tools`; the discovery resources and the
`cwms_get_overview_section` fallback tool are registered directly here.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from cwms_tools import __version__ as PKG_VERSION
from cwms_tools.core.errors import RepairHint
from cwms_tools.core.models import Detail
from cwms_tools.mcp.resources import (
    SERVER_NAME,
    SERVER_TITLE,
    capabilities_payload,
    overview_chunk_payload,
    overview_index_payload,
    overview_section_payload,
)
from cwms_tools.mcp.tools import (
    register_place_tools,
    register_publisher_tools,
    register_value_tools,
)

INSTRUCTIONS = (
    f"{SERVER_TITLE}\n\n"
    "Start with `cwms://capabilities` for the structured server summary "
    "— tools, resources, error codes, fingerprint, and what this server "
    "deliberately does not do. The bundled CWMS orientation document is "
    "indexed at `cwms://overview`; fetch a section via "
    "`cwms://overview/{section_id}{?detail}` or, if your client does not "
    "browse MCP resources, the `cwms_get_overview_section` tool returns "
    "the same content."
)


# --------------------------------------------------------------------------
# Pydantic output schemas for the discovery tool. (Each tool's schemas live
# next to the tool's implementation; capabilities and overview share these
# because their shape is fixed in M3.)
# --------------------------------------------------------------------------


class OverviewChunkRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    byte_range: list[int]
    sha256: str
    has_more: bool


class OverviewSectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    summary: str
    size_bytes: int
    sha256: str
    chunks: list[OverviewChunkRef]
    body: str | None = None
    next_chunk_id: str | None = None


class OverviewSectionError(BaseModel):
    """Response shape returned when a requested overview section or chunk is missing."""

    model_config = ConfigDict(extra="forbid")

    error: str
    repair: RepairHint = Field(
        description="A callable surface (tool name + arguments) the caller should try next.",
    )


def build_server() -> FastMCP:
    """Build a fresh FastMCP server with all v0.1.0 resources and tools registered.

    Returns a new server every call so tests can stand up isolated instances
    without leaking registration state across tests.
    """
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=PKG_VERSION,
    )

    # ----------------------------------------------------------------------
    # Resources
    # ----------------------------------------------------------------------

    @mcp.resource("cwms://capabilities", mime_type="application/json")
    async def _capabilities() -> dict[str, Any]:
        """Structured server summary: name, version, fingerprint, tools, resources,
        error codes, negative scope, and active wrapper-bug workarounds."""
        return capabilities_payload()

    @mcp.resource("cwms://overview", mime_type="application/json")
    async def _overview_index() -> dict[str, Any]:
        """Index of the bundled CWMS orientation document.

        Returns one record per section with title, summary, size, and chunk
        count. Bodies are fetched separately to keep the index cheap.
        """
        return overview_index_payload()

    @mcp.resource(
        "cwms://overview/{section_id}{?detail}",
        mime_type="application/json",
    )
    async def _overview_section(
        section_id: str,
        detail: str = "summary",
    ) -> dict[str, Any]:
        """One section of the bundled CWMS orientation document.

        `detail=summary` (default) returns metadata and the chunk list;
        `detail=full` includes the section body inline (or the first chunk
        when the section is large enough to be chunked).
        """
        payload = overview_section_payload(section_id, detail=detail)
        if payload is None:
            return {
                "error": "section_not_found",
                "section_id": section_id,
                "repair": {
                    "tool": "cwms_get_overview_section",
                    "args": {"section_id": "<one of the listed slugs>"},
                },
            }
        return payload

    @mcp.resource(
        "cwms://overview/{section_id}/chunk/{chunk_id}",
        mime_type="application/json",
    )
    async def _overview_chunk(section_id: str, chunk_id: str) -> dict[str, Any]:
        """One body chunk of an overview section, identified by a stable chunk id.

        Chunk ids come from the `chunks` list returned by the section
        endpoint and stay stable across reads of the same release.
        """
        payload = overview_chunk_payload(section_id, chunk_id)
        if payload is None:
            return {
                "error": "chunk_not_found",
                "section_id": section_id,
                "chunk_id": chunk_id,
                "repair": {
                    "tool": "cwms_get_overview_section",
                    "args": {"section_id": section_id, "detail": "summary"},
                },
            }
        return payload

    # ----------------------------------------------------------------------
    # Tools
    # ----------------------------------------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True, "title": "Get overview section"})
    async def cwms_get_overview_section(
        section_id: Annotated[
            str,
            "Stable slug from the `cwms://overview` index (e.g. 'orientation', "
            "'core-entities', 'gotchas').",
        ],
        detail: Annotated[
            Detail,
            "`summary` returns metadata and the chunk list; `full` returns the "
            "section body (or its first chunk when chunked).",
        ] = Detail.SUMMARY,
        chunk_id: Annotated[
            str | None,
            "When set, returns just that chunk's body. Chunk ids come from the "
            "`chunks` list on a prior section read.",
        ] = None,
    ) -> OverviewSectionResponse | OverviewSectionError:
        """Read one section of the bundled CWMS orientation document.

        Functionally identical to the `cwms://overview/{section_id}`
        resource; use this when the client doesn't browse MCP resources.
        """
        if chunk_id is not None:
            chunk = overview_chunk_payload(section_id, chunk_id)
            if chunk is None:
                return OverviewSectionError(
                    error="chunk_not_found",
                    repair=RepairHint(
                        tool="cwms_get_overview_section",
                        args={"section_id": section_id, "detail": "summary"},
                    ),
                )
            return OverviewSectionResponse(
                section_id=chunk["section_id"],
                title="",  # chunks don't carry a separate title
                summary="",
                size_bytes=chunk["byte_range"][1] - chunk["byte_range"][0],
                sha256=chunk["sha256"],
                chunks=[
                    OverviewChunkRef(
                        chunk_id=chunk["chunk_id"],
                        byte_range=chunk["byte_range"],
                        sha256=chunk["sha256"],
                        has_more=chunk["has_more"],
                    )
                ],
                body=chunk["body"],
                next_chunk_id=None,
            )

        payload = overview_section_payload(section_id, detail=detail.value)
        if payload is None:
            return OverviewSectionError(
                error="section_not_found",
                repair=RepairHint(
                    tool="cwms_get_overview_section",
                    args={"section_id": "<one of the listed slugs>"},
                ),
            )
        return OverviewSectionResponse.model_validate(payload)

    # ----------------------------------------------------------------------
    # Task tools — registered via per-milestone helpers.
    # ----------------------------------------------------------------------
    register_place_tools(mcp)
    register_value_tools(mcp)
    register_publisher_tools(mcp)

    return mcp


__all__ = ["INSTRUCTIONS", "build_server"]
