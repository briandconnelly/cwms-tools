"""FastMCP server construction. Tools register here; resources too.

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
from fastmcp.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field

from cwms_tools import __version__ as PKG_VERSION
from cwms_tools.core import overview
from cwms_tools.core._compact import CompactDumpMixin
from cwms_tools.core.concurrency import run_sync
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint
from cwms_tools.core.models import Detail, ErrorRef, SourceMeta
from cwms_tools.mcp.contract import canonical_fingerprint
from cwms_tools.mcp.output_schema import iserror_output_schema
from cwms_tools.mcp.resources import (
    SERVER_NAME,
    SERVER_TITLE,
    capabilities_payload,
    offices_payload,
    overview_chunk_payload,
    overview_index_payload,
    overview_section_payload,
)
from cwms_tools.mcp.tools import (
    error_ref,
    iserror_aware,
    register_place_tools,
    register_publisher_tools,
    register_value_tools,
    stamp_envelope,
)

INSTRUCTIONS = (
    f"{SERVER_TITLE}\n\n"
    "Start with `cwms://capabilities` for the structured server summary "
    "— tools, resources, error codes, fingerprint, and what this server "
    "deliberately does not do. Valid `office` codes for the tools are "
    "listed at `cwms://offices` (with the NW regional-rollup guidance). "
    "The bundled CWMS orientation document is "
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
    source: SourceMeta


class OverviewIndexEntry(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    summary: str
    size_bytes: int
    sha256: str
    chunk_count: int


class OverviewIndexResponse(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_sha256: str
    sections: list[OverviewIndexEntry]
    source: SourceMeta


class OfficeRecord(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    long_name: str | None = None
    type: str | None = None
    type_label: str | None = None
    reports_to: str | None = None


class OfficesGuidance(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    nw_regional_rollup: str
    nw_district_stubs: list[str]
    nw_rollup_targets: dict[str, str]


class OfficesResponse(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int
    offices: list[OfficeRecord]
    guidance: OfficesGuidance
    partial: bool = Field(
        description="True when a cold-start upstream failure served a fallback slice."
    )


def _overview_source() -> SourceMeta:
    """Provenance for `cwms_get_overview_section`'s success responses (#70) —
    this tool predates the M9 envelope rework and was the one success path
    not carrying `source.fingerprint`, despite this module's own contract."""
    return SourceMeta(fingerprint=canonical_fingerprint())


# JSON-RPC error code for "resource not found": INVALID_PARAMS per SEP-2164,
# matching FastMCP 4's own `resources/read` miss (was -32002 before MCP SDK v2).
_RESOURCE_NOT_FOUND = -32602


def _raise_resource_not_found(
    *, field: str, offending_value: str, message: str, repair: RepairHint
) -> NoReturn:
    """Resource-side failure: a JSON-RPC error carrying the same envelope tools use.

    `resources/read` is a non-tool RPC method, so its semantic failures surface
    through the JSON-RPC envelope rather than an error-shaped success body — but
    `error.data` carries the identical `ErrorEnvelope` tool failures use in
    `structuredContent`, with only the two renames the JSON-RPC carrier requires
    (`code`->`machine_code`, `message`->`human_message`, since native `code`/
    `message` already occupy those keys). One error, one shape, regardless of
    which carrier surfaces it (#64).
    """
    envelope = stamp_envelope(
        CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            message,
            field=field,
            value=offending_value,
            repair=repair,
        ).envelope
    )
    data = envelope.model_dump(mode="json")
    data["machine_code"] = data.pop("code")
    data["human_message"] = data.pop("message")
    raise McpError(code=_RESOURCE_NOT_FOUND, message=message, data=data)


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

    @mcp.resource(
        "cwms://capabilities",
        name="capabilities",
        title="Server capability summary",
        mime_type="application/json",
    )
    async def _capabilities() -> dict[str, Any]:
        """Structured server summary: name, version, fingerprint, tools, resources,
        error codes, negative scope, and active wrapper-bug workarounds."""
        return capabilities_payload()

    @mcp.resource(
        "cwms://offices",
        name="offices",
        title="USACE office directory",
        mime_type="application/json",
    )
    async def _offices() -> dict[str, Any]:
        """USACE office directory for office-code discovery.

        Lists every office (name, long name, type, reporting parent) plus the
        NW regional-rollup guidance. Network-backed (cached 7 days); degrades
        to a documented fallback slice with `partial: true` when upstream is
        unreachable on a cold start.

        The cache-miss fetch is a blocking `cwms-python` call, so it runs on
        the bounded executor (never directly on the event loop) — same policy
        as the task tools. See `core.concurrency`.
        """
        return await run_sync(offices_payload)

    @mcp.resource(
        "cwms://overview",
        name="overview-index",
        title="CWMS orientation document index",
        mime_type="application/json",
    )
    async def _overview_index() -> dict[str, Any]:
        """Index of the bundled CWMS orientation document.

        Returns one record per section with title, summary, size, and chunk
        count. Bodies are fetched separately to keep the index cheap.
        """
        return overview_index_payload()

    @mcp.resource(
        "cwms://overview/{section_id}{?detail}",
        name="overview-section",
        title="CWMS orientation document section",
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
                field="section_id",
                offending_value=section_id,
                message=(
                    f"No overview section {section_id!r}. Valid sections: "
                    f"{', '.join(overview.section_ids())}."
                ),
                repair=RepairHint(
                    next_step="list_overview_sections",
                    tool="cwms_get_overview_section",
                    arguments={},
                ),
            )
        return payload

    @mcp.resource(
        "cwms://overview/{section_id}/chunk/{chunk_id}",
        name="overview-chunk",
        title="CWMS orientation document section chunk",
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
                field="chunk_id",
                offending_value=chunk_id,
                message=(
                    f"No chunk {chunk_id!r} in section {section_id!r}; re-read the "
                    "section for current chunk ids."
                ),
                repair=RepairHint(
                    next_step="reread_section_for_current_chunk_ids",
                    tool="cwms_get_overview_section",
                    arguments={"section_id": section_id, "detail": "summary"},
                ),
            )
        return payload

    # ----------------------------------------------------------------------
    # Tools
    # ----------------------------------------------------------------------

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get overview section",
        },
        output_schema=iserror_output_schema(OverviewSectionResponse, OverviewIndexResponse),
    )
    @iserror_aware
    async def cwms_get_overview_section(
        section_id: Annotated[
            str | None,
            "Stable slug from the `cwms://overview` index (e.g. 'orientation', "
            "'gotchas'). Omit to get the index itself.",
        ] = None,
        detail: Annotated[
            Detail,
            "`summary`: metadata + chunk list. `full`: section body (or first "
            "chunk). Ignored when `section_id` is omitted.",
        ] = Detail.SUMMARY,
        chunk_id: Annotated[
            str | None,
            "Returns just that chunk's body; ids come from a prior section "
            "read's `chunks` list. Requires `section_id`.",
        ] = None,
    ) -> OverviewSectionResponse | OverviewIndexResponse | ErrorRef:
        """Read the bundled CWMS orientation document, or one section of it.

        The primary escape hatch for clients that can't browse MCP resources:
        no args = the `cwms://overview` index; `section_id` = that section
        (matches `cwms://overview/{section_id}`); + `chunk_id` = one chunk's
        body. Missing section/chunk returns `{ok: false, error: {...}}`
        (code `not_found`).
        """
        if section_id is None:
            if chunk_id is not None:
                return error_ref(
                    CwmsToolsError.of(
                        ErrorCode.USAGE_ERROR,
                        "chunk_id requires section_id.",
                        field="chunk_id",
                        value=chunk_id,
                        reason=(
                            "Pass section_id along with chunk_id, or omit both to get the index."
                        ),
                    )
                )
            return OverviewIndexResponse.model_validate(
                {**overview_index_payload(), "source": _overview_source()}
            )

        if chunk_id is not None:
            chunk = overview_chunk_payload(section_id, chunk_id)
            if chunk is None:
                return error_ref(
                    CwmsToolsError.of(
                        ErrorCode.NOT_FOUND,
                        f"No chunk {chunk_id!r} in section {section_id!r}.",
                        field="chunk_id",
                        value=chunk_id,
                        repair=RepairHint(
                            next_step="reread_section_for_current_chunk_ids",
                            tool="cwms_get_overview_section",
                            arguments={"section_id": section_id, "detail": "summary"},
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
                source=_overview_source(),
            )

        payload = overview_section_payload(section_id, detail=detail.value)
        if payload is None:
            return error_ref(
                CwmsToolsError.of(
                    ErrorCode.NOT_FOUND,
                    f"No overview section {section_id!r}. Valid sections: "
                    f"{', '.join(overview.section_ids())}.",
                    field="section_id",
                    value=section_id,
                    repair=RepairHint(
                        next_step="list_overview_sections",
                        tool="cwms_get_overview_section",
                        arguments={},
                    ),
                )
            )
        return OverviewSectionResponse.model_validate({**payload, "source": _overview_source()})

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "title": "List USACE offices",
        },
        output_schema=iserror_output_schema(OfficesResponse),
    )
    @iserror_aware
    async def cwms_list_offices() -> OfficesResponse | ErrorRef:
        """List USACE office codes for the `office` argument, with NW regional-rollup guidance.

        Fallback for clients that can't browse MCP resources — same content
        as `cwms://offices`, including NW rollup guidance (query NWDM/NWDP,
        not the NWO/NWK/NWS/NWP/NWW stubs).
        """
        payload = await run_sync(offices_payload)
        return OfficesResponse.model_validate(payload)

    # ----------------------------------------------------------------------
    # Task tools — registered via per-milestone helpers.
    # ----------------------------------------------------------------------
    register_place_tools(mcp)
    register_value_tools(mcp)
    register_publisher_tools(mcp)

    return mcp


__all__ = ["INSTRUCTIONS", "build_server"]
