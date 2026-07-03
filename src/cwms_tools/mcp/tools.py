"""MCP tool registrations.

Tools land here as `register_*` functions invoked from `mcp/server.py`. Each
function takes the FastMCP instance and adds its tools. Keeping the
registration out of the top-level `build_server` keeps that function
declarative and short as more tools land in later milestones.

Every successful tool response carries a `source.fingerprint` field (the
capability fingerprint at call time). Semantic handler failures (the
`CwmsToolsError` → `ErrorRef` paths: not-found, ghost-office, usage/field
validation we perform, upstream errors) carry the structured
`{ok: false, error: {...}}` envelope in `structuredContent` AND set protocol
`isError: true` (via `iserror_aware`); the envelope stays the discriminator.
Malformed-argument errors raised by FastMCP/pydantic *before* a handler runs
(wrong type, missing required arg, out-of-enum value) never reach `iserror_aware`
and surface as plain protocol errors without the envelope.
"""

from __future__ import annotations

import functools
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from cwms_tools.core import concurrency, places, publishers_index, shaping, values
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, ErrorEnvelope
from cwms_tools.core.geo import BBox
from cwms_tools.core.models import (
    BrowseRegionResponse,
    DescribePlaceResponse,
    Detail,
    ErrorRef,
    HistoryResponse,
    ListParametersResponse,
    ProfileResponse,
    PublishersForParameterResponse,
    Rollup,
    SearchPlacesResponse,
    SourceMeta,
    ValueWithContextResponse,
)
from cwms_tools.mcp.contract import canonical_fingerprint
from cwms_tools.mcp.output_schema import iserror_output_schema

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Shared param prose (#67): defined once so every tool's copy stays in sync
#: and short, instead of each repeating a full paragraph in its own schema.
_OFFICE_HINT = "USACE office code. Discover valid codes at `cwms://offices`."
_CURSOR_HINT = (
    "Opaque cursor from a prior `next_cursor`; omit for page one. A stale "
    "cursor returns `invalid_cursor` — retry without it."
)
_PARAMETER_HINT = (
    "Parameter code (e.g. Temp-Water, Elev, Flow-In). Case-sensitive; see `cwms_list_parameters`."
)
_UNIT_HINT = "Unit system: 'EN' (ft, cfs) or 'SI' (m, cms)."


def _source(
    workaround: str | None = None,
    upstream_status: int | None = None,
) -> SourceMeta:
    """Build the per-response provenance, including the capability fingerprint.

    `upstream_status` propagates the HTTP status of any recovered partial-success
    sub-call so agents can see how the response degraded (e.g. 404 from the
    project lookup on a non-project location).
    """
    return SourceMeta(
        fingerprint=canonical_fingerprint(),
        workaround=workaround,
        upstream_status=upstream_status,
    )


def stamp_envelope(envelope: ErrorEnvelope) -> ErrorEnvelope:
    """Stamp the capability fingerprint and JSON-RPC request id onto an error envelope.

    The single provenance-stamping path for *both* error carriers (#64): the
    in-band tool envelope via `error_ref()` below, and the JSON-RPC `error.data`
    envelope for resource failures in `mcp/server.py`. Keeping this in one place
    means both carriers carry identical `source.fingerprint`/`protocol_request_id`
    provenance rather than each surface growing its own stamping logic.

    When called from inside a live FastMCP request context, `protocol_request_id`
    is populated with the JSON-RPC message id so agents can correlate the error
    envelope against their client-side logs. The field is absent (None, stripped by
    CompactDumpMixin) when called outside a request context (e.g. direct unit-test
    invocation via `server.call_tool`).
    """
    envelope.source.fingerprint = canonical_fingerprint()
    try:
        from fastmcp.server.dependencies import get_context  # noqa: PLC0415

        envelope.protocol_request_id = str(get_context().request_id)
    except (ImportError, RuntimeError, AttributeError):
        pass  # not in a request context (e.g. direct unit-test invocation)
    return envelope


def error_ref(err: CwmsToolsError) -> ErrorRef:
    """Build the in-band error envelope with provenance stamped (see `stamp_envelope`)."""
    ref = ErrorRef.from_error(err)
    stamp_envelope(ref.error)
    return ref


def _error_tool_result(ref: ErrorRef) -> ToolResult:
    """Wrap an in-band `{ok: false}` envelope so the failure also sets the
    protocol-level `isError: true` flag (#19), and matches the schema-declared
    `x-fastmcp-wrap-result` carrier shape (#64).

    The structured envelope remains the stable, branchable contract — agents
    discriminate on the `ok` field — and `isError` is an additive signal layered
    on top via FastMCP 3.4.x's `ToolResult(is_error=...)`. The text content
    mirrors the (unwrapped) JSON envelope so non-structured clients see the same
    payload without needing to know about the wrap convention.

    Every `iserror_aware`-decorated tool declares a `SomeResponse | ErrorRef`
    return annotation, a Union FastMCP cannot flatten into a single top-level
    object schema — so FastMCP always sets `x-fastmcp-wrap-result: true` on
    these tools' outputSchema and wraps *success* responses as
    `{"result": ...}`. `ToolResult(structured_content=...)` bypasses that
    automatic wrapping, so this mirrors it explicitly on the error path too
    (FastMCP 3.4.x's own `Tool._convert_result`, `tools/base.py`). A parametrized
    test (`test_mcp_tool_handlers.py`) guards the invariant that every
    `iserror_aware` tool's outputSchema is wrap-flagged, so this assumption
    fails loudly rather than silently if it ever stops holding.
    """
    envelope = ref.model_dump(mode="json")
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope))],
        structured_content={"result": envelope},
        meta={"fastmcp": {"wrap_result": True}},
        is_error=True,
    )


def iserror_aware(fn):
    """Decorate a tool handler so a returned `ErrorRef` becomes a protocol error.

    Handlers keep returning `ErrorRef` (the single internal error currency); this
    wrapper converts that to `ToolResult(is_error=True, ...)` at the protocol
    boundary while leaving success responses untouched. `functools.wraps`
    preserves the signature and return annotation FastMCP introspects to build
    the input/output schemas.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if isinstance(result, ErrorRef):
            return _error_tool_result(result)
        return result

    # Runtime marker so tests can assert every registered tool actually has this
    # decorator applied (#64: cwms_get_profile was silently missing it — a gap
    # `functools.wraps`-preserved signatures can't otherwise detect from outside).
    setattr(wrapper, "__iserror_aware__", True)  # noqa: B010
    return wrapper


def register_place_tools(mcp: FastMCP) -> None:
    """Register the place-related tools on the FastMCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Search places by name",
        },
        output_schema=iserror_output_schema(SearchPlacesResponse),
    )
    @iserror_aware
    async def cwms_search_places(
        query: Annotated[str, "Name fragment to match, case-insensitive."],
        office: Annotated[
            str | list[str] | None,
            "Office code(s); omit to fan out across already-cached offices. "
            "Uncached overflow lands in `offices_skipped_for_budget`. Omit if "
            "unknown — an empty result's `repair_hint` names offices to retry.",
        ] = None,
        parameter: Annotated[
            str | None,
            "Filter to locations publishing this parameter; ghost parents "
            "with a `data_at` match are kept. Drop count in "
            "`nearby_non_matching_count`.",
        ] = None,
        limit: Annotated[
            int,
            "Result cap (default 50; 0 = no cap). When hit, sets `truncated`/"
            "`total_count`/`has_more`/`next_cursor` for the next page.",
        ] = places.DEFAULT_SEARCH_LIMIT,
        cursor: Annotated[str | None, _CURSOR_HINT] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> SearchPlacesResponse | ErrorRef:
        """Resolve a CWMS place name to ranked location matches.

        For ambiguous name lookup. With a known `office` + `name`, call
        `cwms_describe_place`, `cwms_list_parameters`, or `cwms_get_value`/
        `cwms_get_history` instead.

        Data-bearing records sort first; each carries `data_at` co-located
        repair hints when it's a ghost.
        """
        if limit < 0:
            return error_ref(_negative_limit_error(limit))
        effective_limit = None if limit == 0 else limit
        raw = await _safe(
            places.search_places,
            query,
            office=office,
            parameter=parameter,
            limit=effective_limit,
            cursor=cursor,
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_place_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return SearchPlacesResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Describe a place",
        },
        output_schema=iserror_output_schema(DescribePlaceResponse),
    )
    @iserror_aware
    async def cwms_describe_place(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, "Location id within the office (e.g. FTPK, FOSS)."],
        detail: Detail = Detail.SUMMARY,
    ) -> DescribePlaceResponse | ErrorRef:
        """Read everything about one place in a single call.

        Location record, project metadata, parameters grouped by publisher,
        and last data timestamp. Sets `partial`/`partial_reasons` when a
        sub-lookup degrades (e.g. a project-record format error).
        """
        raw = await _safe(places.describe_place, office, name)
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_place_detail(raw, detail)
        workaround = shaped.get("source_workaround")
        upstream_status = shaped.get("upstream_status")
        shaped["source"] = _source(
            workaround=workaround,
            upstream_status=upstream_status,
        ).model_dump(mode="json")
        return DescribePlaceResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "List parameters at a place",
        },
        output_schema=iserror_output_schema(ListParametersResponse),
    )
    @iserror_aware
    async def cwms_list_parameters(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, "Location id within the office."],
        detail: Detail = Detail.SUMMARY,
    ) -> ListParametersResponse | ErrorRef:
        """List the parameters published at a location, grouped by publisher.

        The cheapest ghost probe: a ghost returns `ts_count: 0` and an empty
        `by_publisher` list.
        """
        raw = await _safe(places.list_parameters, office, name)
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_place_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ListParametersResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Browse a region's catalog",
        },
        output_schema=iserror_output_schema(BrowseRegionResponse),
    )
    @iserror_aware
    async def cwms_browse_region(
        office: Annotated[str, _OFFICE_HINT],
        south: Annotated[float | None, "Bounding box south latitude, decimal degrees."] = None,
        west: Annotated[float | None, "Bounding box west longitude, decimal degrees."] = None,
        north: Annotated[float | None, "Bounding box north latitude, decimal degrees."] = None,
        east: Annotated[float | None, "Bounding box east longitude, decimal degrees."] = None,
        state: Annotated[str | None, "Two-letter US state code (e.g. MT, OK)."] = None,
        limit: Annotated[
            int,
            "Result cap (default 50; 0 = no cap). Data-bearing rows sort ahead "
            "of ghosts. When hit, sets `truncated`/`has_more`/`next_cursor`.",
        ] = places.DEFAULT_BROWSE_LIMIT,
        cursor: Annotated[str | None, _CURSOR_HINT] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> BrowseRegionResponse | ErrorRef:
        """Browse the locations published by one office, optionally filtered.

        Same enriched per-place records as `cwms_search_places` (including
        `data_at`), with `result_count`/`ghost_count`/`total_count` totals.
        The bounding-box filter requires all four corners or none.
        """
        bbox: BBox | None = None
        provided = [v for v in (south, west, north, east) if v is not None]
        if len(provided) not in {0, 4}:
            return error_ref(
                CwmsToolsError.of(
                    ErrorCode.USAGE_ERROR,
                    "When specifying a bounding box, all four of south, west, "
                    "north, east must be provided.",
                    field="bbox",
                    offending_value={
                        "south": south,
                        "west": west,
                        "north": north,
                        "east": east,
                    },
                    hint="Pass all four bbox edges or omit bbox entirely.",
                )
            )
        if south is not None and west is not None and north is not None and east is not None:
            bbox = BBox(south=south, west=west, north=north, east=east)
        if limit < 0:
            return error_ref(_negative_limit_error(limit))
        effective_limit = None if limit == 0 else limit
        raw = await _safe(
            places.browse_region,
            office=office,
            bbox=bbox,
            state=state,
            limit=effective_limit,
            cursor=cursor,
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_place_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return BrowseRegionResponse.model_validate(shaped)


def register_value_tools(mcp: FastMCP) -> None:
    """Register the value-related tools on the FastMCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Current value (optional status)",
        },
        output_schema=iserror_output_schema(ValueWithContextResponse),
    )
    @iserror_aware
    async def cwms_get_value(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[
            str,
            "CWMS location name/id within the office (e.g. FTPK, FOSS, "
            "or a depth-tagged sensor like UBLW_S1-D21,0ft).",
        ],
        parameter: Annotated[str, _PARAMETER_HINT],
        window_hours: Annotated[
            int,
            "How far back to search for the most recent value, in hours.",
        ] = 24,
        unit: Annotated[Literal["EN", "SI"], _UNIT_HINT] = "EN",
        with_status: Annotated[
            bool,
            "Classify against CWMS Location Levels. OFF by default (slow, "
            "often times out on cold cache); `level_lookup_status` reports "
            "what happened either way.",
        ] = False,
        detail: Detail = Detail.SUMMARY,
    ) -> ValueWithContextResponse | ErrorRef:
        """Latest observation for a parameter at a place.

        Value-only and fast by default; `with_status=true` also classifies
        against thresholds but is slow and often times out.

        Auto-selects the canonical timeseries id. On successful
        classification, carries `status_class` and `thresholds_active`.
        """
        raw = await _safe(
            values.get_value,
            office,
            name,
            parameter,
            window=timedelta(hours=window_hours),
            unit=unit,
            classify_against_levels=with_status,
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_value_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ValueWithContextResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Windowed history",
        },
        output_schema=iserror_output_schema(HistoryResponse),
    )
    @iserror_aware
    async def cwms_get_history(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, "CWMS location name/id within the office (e.g. FTPK, FOSS)."],
        parameter: Annotated[str, _PARAMETER_HINT],
        begin_iso: Annotated[
            str, "Window start as an RFC3339 timestamp (e.g. 2026-05-17T00:00:00Z)."
        ],
        end_iso: Annotated[
            str,
            "Window end as an RFC3339 timestamp (e.g. 2026-05-18T00:00:00Z).",
        ],
        unit: Annotated[Literal["EN", "SI"], _UNIT_HINT] = "EN",
        rollup: Annotated[
            Rollup,
            "Downsample: 'raw' (default) or 'hourly'/'daily' (per-bucket "
            "aggregates in `buckets`, exempt from the local raw-point cap — "
            "but the upstream fetch cap can still apply; see `truncated`).",
        ] = Rollup.RAW,
        detail: Detail = Detail.SUMMARY,
    ) -> HistoryResponse | ErrorRef:
        """Read observations across a bounded time window.

        For a value series over time; for latest value + status, use
        `cwms_get_value` instead (cheaper).

        Read `summary` for "how has X changed", or `rollup='hourly'`/`'daily'`
        for compact aggregates. If `truncated`, check `truncation_hint`: a
        local raw-point cap still covers the full window (rollup gives a
        complete summary), but an upstream fetch cap means `summary`/
        `buckets` cover only the fetched prefix — continue via `next_begin`
        until `truncated` is false.
        """
        try:
            begin = datetime.fromisoformat(begin_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            return error_ref(
                CwmsToolsError.of(
                    ErrorCode.INVALID_FIELD,
                    f"Could not parse begin_iso as RFC3339: {exc}",
                    field="begin_iso",
                    offending_value=begin_iso,
                    hint="RFC3339 with timezone, e.g. 2026-05-17T00:00:00Z",
                )
            )
        try:
            end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            return error_ref(
                CwmsToolsError.of(
                    ErrorCode.INVALID_FIELD,
                    f"Could not parse end_iso as RFC3339: {exc}",
                    field="end_iso",
                    offending_value=end_iso,
                    hint="RFC3339 with timezone, e.g. 2026-05-18T00:00:00Z",
                )
            )
        raw = await _safe(
            values.get_history,
            office,
            name,
            parameter,
            begin=begin,
            end=end,
            unit=unit,
            rollup=rollup.value,
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_history_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return HistoryResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Depth profile (whole string)",
        },
        output_schema=iserror_output_schema(ProfileResponse),
    )
    @iserror_aware
    async def cwms_get_profile(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[
            str,
            "Parent 'string' location id (e.g. GWLW_S1) — NOT a single "
            "depth-tagged sensor; finds the sensors hanging off this string.",
        ],
        parameter: Annotated[
            str,
            "Parameter code to read at each depth (commonly Temp-Water). Case-sensitive.",
        ],
        window_hours: Annotated[
            int,
            "How far back to search for each sensor's most recent value, in hours.",
        ] = 24,
        unit: Annotated[Literal["EN", "SI"], "Unit system: 'EN' (ft, °F) or 'SI' (m, °C)."] = "EN",
        detail: Detail = Detail.SUMMARY,
    ) -> ProfileResponse | ErrorRef:
        """Read every depth sensor of one string in a single call.

        For a vertical profile (e.g. water-temperature stratification);
        replaces one `cwms_get_value` call per depth. A dead sensor
        degrades to `value: null` + `error` rather than failing the whole
        profile; when none match, `note` explains recovery.
        """
        raw = await _safe(
            values.get_profile,
            office,
            name,
            parameter,
            window=timedelta(hours=window_hours),
            unit=unit,
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_profile_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ProfileResponse.model_validate(shaped)


def register_publisher_tools(mcp: FastMCP) -> None:
    """Register the publisher-related helper tools on the FastMCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Publishers reporting a parameter",
        },
        output_schema=iserror_output_schema(PublishersForParameterResponse),
    )
    @iserror_aware
    async def cwms_publishers_for_parameter(
        parameter: Annotated[str, "Parameter code (e.g. Elev, Flow-In, Flow-Out, Stage)."],
        offices: Annotated[
            list[str] | None,
            "Limit the index to these office codes. If omitted, only offices "
            "already cached are scanned — never expands to every office.",
        ] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> PublishersForParameterResponse | ErrorRef:
        """List the publishers reporting a parameter, with explicit coverage.

        Indexes `offices`, or only already-cached offices when omitted —
        never fans out to every office. Budget overflow lands in
        `coverage.offices_skipped_for_budget` with a `repair` hint back here.
        """
        raw = await _safe(
            publishers_index.publishers_for_parameter,
            parameter,
            offices=offices,
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_publishers_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return PublishersForParameterResponse.model_validate(shaped)


# Detail toggle shaping lives in `core.shaping` so the CLI and MCP surfaces apply
# the exact same `summary`/`full` pruning (see #56). Call sites use
# `shaping.shape_*_detail(...)` directly.


def _negative_limit_error(limit: int) -> CwmsToolsError:
    """Usage error for a negative `limit`. Validated in the handler because the
    core raises a plain `ValueError` that `_safe` (CwmsToolsError-only) won't catch."""
    return CwmsToolsError.of(
        ErrorCode.USAGE_ERROR,
        "limit must be a non-negative integer (0 means no cap).",
        field="limit",
        offending_value=limit,
        hint="Pass limit=0 for no cap, or any non-negative integer.",
    )


async def _safe(fn, *args, **kwargs) -> dict[str, Any] | ErrorRef:
    """Run a sync core function on the bounded executor; surface known errors structured.

    Returns the raw dict on success, or an `ErrorRef` (with fingerprint stamped)
    on any `CwmsToolsError`. Handlers check `isinstance(raw, ErrorRef)` and
    return it directly.
    """
    try:
        return await concurrency.run_sync(fn, *args, **kwargs)
    except CwmsToolsError as err:
        return error_ref(err)


__all__ = [
    "error_ref",
    "iserror_aware",
    "register_place_tools",
    "register_publisher_tools",
    "register_value_tools",
    "stamp_envelope",
]
