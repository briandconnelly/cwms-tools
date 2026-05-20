"""FastMCP 3 server construction. Tools register here; resources too.

Importing this module is side-effect-free — `build_server()` is the only
factory and it returns a freshly-configured FastMCP instance. The CLI
`mcp serve` subcommand imports this factory and runs it.

Task tools (place, value, region, publisher) register via the helpers
in `cwms_tools.mcp.tools`; the discovery resources and the
`cwms_get_overview_section` fallback tool are registered directly here.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastmcp import FastMCP
from mcp import McpError
from mcp.types import ErrorData
from pydantic import BaseModel, ConfigDict

from cwms_tools import __version__ as PKG_VERSION
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint
from cwms_tools.core.models import Detail, ErrorRef
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


# JSON-RPC error code for "resource not found" (MCP convention).
_RESOURCE_NOT_FOUND = -32002


def _raise_resource_not_found(
    *, machine_code: str, human_message: str, repair: dict[str, Any]
) -> NoReturn:
    """Resource-side failure: a JSON-RPC error carrying repair fields in `error.data`.

    `resources/read` is a non-tool RPC method, so its semantic failures surface
    through the JSON-RPC envelope (not an error-shaped success body). The repair
    contract rides in `error.data` so agents can branch without parsing prose.
    """
    raise McpError(
        ErrorData(
            code=_RESOURCE_NOT_FOUND,
            message=human_message,
            data={
                "machine_code": machine_code,
                "human_message": human_message,
                "repair": repair,
                "recoverable": False,
            },
        )
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
            _raise_resource_not_found(
                machine_code="section_not_found",
                human_message=(
                    f"No overview section {section_id!r}; read cwms://overview for slugs."
                ),
                repair={
                    "tool": "cwms_get_overview_section",
                    "args": {"section_id": "<one of the listed slugs>"},
                },
            )
        return payload

    @mcp.resource(
        "cwms://overview/{section_id}/chunk/{chunk_id}",
        mime_type="application/json",
    )
    async def _overview_chunk(section_id: str, chunk_id: str) -> dict[str, Any]:
        """One body chunk of an overview section, identified by a stable chunk id.

        Chunk ids come from the `chunks` list returned by the section
        endpoint. They remain stable for the same server fingerprint
        (visible at `cwms://capabilities.fingerprint`); a fingerprint
        change indicates new chunk ids.
        """
        payload = overview_chunk_payload(section_id, chunk_id)
        if payload is None:
            _raise_resource_not_found(
                machine_code="chunk_not_found",
                human_message=(
                    f"No chunk {chunk_id!r} in section {section_id!r}; re-read the "
                    "section for current chunk ids."
                ),
                repair={
                    "tool": "cwms_get_overview_section",
                    "args": {"section_id": section_id, "detail": "summary"},
                },
            )
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
    ) -> OverviewSectionResponse | ErrorRef:
        """Read one section of the bundled CWMS orientation document.

        Use this fallback when the client can't browse MCP resources.
        Without `chunk_id` the response matches the `cwms://overview/
        {section_id}` resource; with `chunk_id` it returns that single
        chunk's body (with `title`/`summary` empty since they apply to
        the section, not the chunk). On a missing section/chunk it returns
        the standard `{ok: false, error: {...}}` envelope (code `not_found`),
        the same shape every task tool uses.
        """
        if chunk_id is not None:
            chunk = overview_chunk_payload(section_id, chunk_id)
            if chunk is None:
                return ErrorRef.from_error(
                    CwmsToolsError.of(
                        ErrorCode.NOT_FOUND,
                        f"No chunk {chunk_id!r} in section {section_id!r}.",
                        field="chunk_id",
                        offending_value=chunk_id,
                        repair=RepairHint(
                            tool="cwms_get_overview_section",
                            args={"section_id": section_id, "detail": "summary"},
                        ),
                    )
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
            return ErrorRef.from_error(
                CwmsToolsError.of(
                    ErrorCode.NOT_FOUND,
                    f"No overview section {section_id!r}; read cwms://overview for slugs.",
                    field="section_id",
                    offending_value=section_id,
                    repair=RepairHint(
                        tool="cwms_get_overview_section",
                        args={"section_id": "<one of the listed slugs>"},
                    ),
                )
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
