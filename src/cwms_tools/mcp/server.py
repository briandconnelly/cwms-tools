"""FastMCP 3 server construction. Tools register here; resources too.

Importing this module is side-effect-free — `build_server()` is the only
factory and it returns a freshly-configured FastMCP instance. The CLI
`mcp serve` subcommand (M7) imports this factory and runs it.

In v0.1.0 the only tool registered is `cwms_get_overview_section` (the
discovery-tool fallback). Place / value / region tools land in M4-M6.
"""

from __future__ import annotations

from typing import Any

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
from cwms_tools.mcp.tools import register_place_tools

INSTRUCTIONS = (
    f"{SERVER_TITLE}\n\n"
    "Read `cwms://capabilities` first for the structured server summary "
    "(tools, resources, error codes, fingerprint, negative scope). "
    "The overview index at `cwms://overview` lists the section IDs you can "
    "fetch as `cwms://overview/{section_id}{?detail}` or via the "
    "`cwms_get_overview_section` tool fallback for resource-poor clients."
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
    """Returned (as the `isError` payload) when a section/chunk isn't found."""

    model_config = ConfigDict(extra="forbid")

    error: str
    repair: RepairHint = Field(
        description="Pointer to a next call that should succeed.",
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
        """Server capability summary + fingerprint."""
        return capabilities_payload()

    @mcp.resource("cwms://overview", mime_type="application/json")
    async def _overview_index() -> dict[str, Any]:
        """Index of overview sections; bodies are NOT included here."""
        return overview_index_payload()

    @mcp.resource(
        "cwms://overview/{section_id}{?detail}",
        mime_type="application/json",
    )
    async def _overview_section(
        section_id: str,
        detail: str = "summary",
    ) -> dict[str, Any]:
        """One overview section, with summary metadata and optional body."""
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
        """A single body chunk of an overview section, keyed by stable chunk_id."""
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
        section_id: str,
        detail: Detail = Detail.SUMMARY,
        chunk_id: str | None = None,
    ) -> OverviewSectionResponse | OverviewSectionError:
        """Read one section of the bundled CWMS overview.

        Use this when your client doesn't surface MCP resources well — the
        shape matches `cwms://overview/{section_id}` exactly so the same
        parser handles both paths.

        - `section_id`: stable slug from `cwms://overview` index.
        - `detail`: `summary` (default) returns metadata + chunk list;
          `full` returns the body (or the first chunk if chunked).
        - `chunk_id`: when set, returns just that one chunk's body.
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

    return mcp


__all__ = ["INSTRUCTIONS", "build_server"]
