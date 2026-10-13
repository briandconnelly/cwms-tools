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
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from cwms_tools.core import concurrency, offices, places, publishers_index, shaping, values
from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    ErrorEnvelope,
    resolve_repair_intent,
    surface_field_name,
)
from cwms_tools.core.geo import BBox, first_missing_bbox_field
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
_OFFICE_HINT = "USACE office code, case-insensitive (e.g. SWT). Valid codes: `cwms://offices`."
_NAME_HINT = (
    "Case-sensitive catalog id (FOSS, not 'Foss'), from `cwms_search_places`; "
    "keep commas (UBLW_S1-D21,0ft)."
)
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
    provenance rather than each surface growing its own stamping logic. Also the
    single place both carriers get `field` translated to the surface parameter
    name (#68) — core producers emit whatever internal name suits their own
    domain (e.g. `office_id`); `surface_field_name()` maps it to what an agent
    would actually retry with (e.g. `office`).

    When called from inside a live FastMCP request context, `protocol_request_id`
    is populated with the JSON-RPC message id so agents can correlate the error
    envelope against their client-side logs. The field is absent (None, stripped by
    CompactDumpMixin) when called outside a request context (e.g. direct unit-test
    invocation via `server.call_tool`).
    """
    envelope.source.fingerprint = canonical_fingerprint()
    if envelope.details is not None:
        envelope.details.field = surface_field_name(envelope.details.field)
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
    on top via FastMCP's `ToolResult(is_error=...)` (since 3.4). The text content
    mirrors the (unwrapped) JSON envelope so non-structured clients see the same
    payload without needing to know about the wrap convention.

    Every `iserror_aware`-decorated tool declares a `SomeResponse | ErrorRef`
    return annotation, a Union FastMCP cannot flatten into a single top-level
    object schema — so FastMCP always sets `x-fastmcp-wrap-result: true` on
    these tools' outputSchema and wraps *success* responses as
    `{"result": ...}`. `ToolResult(structured_content=...)` bypasses that
    automatic wrapping, so this mirrors it explicitly on the error path too
    (FastMCP's own success-path wrapping in `tools/base.py`). A parametrized
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
            "title": "Search places by name",
        },
        output_schema=iserror_output_schema(SearchPlacesResponse),
    )
    @iserror_aware
    async def cwms_search_places(
        query: Annotated[str, "Name fragment to match, case-insensitive."],
        office: Annotated[
            str | list[str] | None,
            "Office code(s), case-insensitive. If unknown, omit it: on a fresh "
            "server the first call returns no rows and a `repair_hint`; keep "
            "following `repair_hint` while one is present (reason "
            "`match_found_offices_skipped`: optional). No hint left is not proof "
            "of absence (`scope_note`). If you "
            "know the region, pass it "
            "directly (NWDM Missouri mainstem, NWDP Columbia/Snake, SWT "
            "Oklahoma, MVS St. Louis; full list at `cwms://offices`).",
        ] = None,
        state: Annotated[
            str | None,
            "Two-letter state code (CA, PR): searches the districts covering it "
            "(`cwms://offices` coverage). Not with `office`; rows may lie outside it.",
        ] = None,
        parameter: Annotated[
            str | None,
            "Filter to locations publishing this parameter; ghost parents "
            "with a `data_at` match are kept. Drop count in "
            "`nearby_non_matching_count`.",
        ] = None,
        limit: Annotated[
            int,
            "Result cap (default 50; 0 = no cap). When hit, sets `total_count`/"
            "`has_more`/`next_cursor` for the next page (fully pageable — "
            "`truncated` stays false).",
        ] = places.DEFAULT_SEARCH_LIMIT,
        cursor: Annotated[str | None, _CURSOR_HINT] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> SearchPlacesResponse | ErrorRef:
        """Resolve a CWMS place name to ranked location matches.

        For ambiguous name lookup. With a known `office` + `name`, call
        `cwms_describe_place`, `cwms_list_parameters`, or `cwms_get_value`/
        `cwms_get_history` instead.

        Data-bearing records sort first; ghosts carry `data_at` hints.
        """
        if limit < 0:
            return error_ref(_negative_limit_error(limit))
        effective_limit = None if limit == 0 else limit
        raw = await _safe(
            places.search_places,
            query,
            office=office,
            state=state,
            parameter=parameter,
            limit=effective_limit,
            cursor=cursor,
            _repair_call=(
                "cwms_search_places",
                {
                    **({"office": office} if office is not None else {}),
                    **({"state": state} if state is not None else {}),
                    "query": query,
                    **({"parameter": parameter} if parameter is not None else {}),
                    "limit": limit,
                    **({"cursor": cursor} if cursor is not None else {}),
                    "detail": detail.value,
                },
            ),
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
            "title": "Describe a place",
        },
        output_schema=iserror_output_schema(DescribePlaceResponse),
    )
    @iserror_aware
    async def cwms_describe_place(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, _NAME_HINT],
        include_locks: Annotated[
            bool,
            "Add navigation-lock dimensions for a PROJECT (1 list call + 1 per "
            "lock) or LOCK (1 call) location.",
        ] = False,
        detail: Detail = Detail.SUMMARY,
    ) -> DescribePlaceResponse | ErrorRef:
        """Read everything about one place in a single call.

        Location record, project metadata, parameters grouped by publisher,
        and last data timestamp. Sets `partial`/`partial_reasons` when a
        sub-lookup degrades (e.g. the location is not a project).
        """
        raw = await _safe(
            places.describe_place,
            office,
            name,
            include_locks=include_locks,
            _repair_call=(
                "cwms_describe_place",
                {
                    "office": office,
                    "name": name,
                    **({"include_locks": True} if include_locks else {}),
                    "detail": detail.value,
                },
            ),
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_place_detail(raw, detail)
        # These carry the project-lookup recovery signal into `source` below;
        # pop rather than leave them (they'd otherwise duplicate
        # source.workaround/source.upstream_status at the top level, #74).
        workaround = shaped.pop("source_workaround", None)
        upstream_status = shaped.pop("upstream_status", None)
        shaped["source"] = _source(
            workaround=workaround,
            upstream_status=upstream_status,
        ).model_dump(mode="json")
        return DescribePlaceResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "title": "List parameters at a place",
        },
        output_schema=iserror_output_schema(ListParametersResponse),
    )
    @iserror_aware
    async def cwms_list_parameters(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, _NAME_HINT],
        detail: Detail = Detail.SUMMARY,
    ) -> ListParametersResponse | ErrorRef:
        """List the parameters published at a location, grouped by publisher.

        The cheapest ghost probe: a ghost returns `ts_count: 0` and an empty
        `by_publisher` list.
        """
        raw = await _safe(
            places.list_parameters,
            office,
            name,
            _repair_call=(
                "cwms_list_parameters",
                {"office": office, "name": name, "detail": detail.value},
            ),
        )
        if isinstance(raw, ErrorRef):
            return raw
        shaped = shaping.shape_place_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ListParametersResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
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
        kind: Annotated[
            str | None,
            "CDA location kind, e.g. PROJECT, LOCK, SITE, STREAM (case-insensitive). "
            "PROJECT follows the catalog, not /projects.",
        ] = None,
        limit: Annotated[
            int,
            "Result cap (default 50; 0 = no cap). Data-bearing rows sort ahead "
            "of ghosts. When hit, sets `has_more`/`next_cursor` (fully "
            "pageable — `truncated` stays false).",
        ] = places.DEFAULT_BROWSE_LIMIT,
        cursor: Annotated[str | None, _CURSOR_HINT] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> BrowseRegionResponse | ErrorRef:
        """Browse the locations published by one office, optionally filtered.

        Same enriched per-place records as `cwms_search_places`, with
        `result_count`/`ghost_count`/`total_count` totals.
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
                    field=first_missing_bbox_field(south, west, north, east),
                    value={
                        "south": south,
                        "west": west,
                        "north": north,
                        "east": east,
                    },
                    reason="Pass all four bbox edges or omit bbox entirely.",
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
            kind=kind,
            limit=effective_limit,
            cursor=cursor,
            _repair_call=(
                "cwms_browse_region",
                {
                    "office": office,
                    **(
                        {"south": south, "west": west, "north": north, "east": east}
                        if bbox is not None
                        else {}
                    ),
                    **({"state": state} if state is not None else {}),
                    **({"kind": kind} if kind is not None else {}),
                    "limit": limit,
                    **({"cursor": cursor} if cursor is not None else {}),
                    "detail": detail.value,
                },
            ),
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
            "title": "Current value (optional status)",
        },
        output_schema=iserror_output_schema(ValueWithContextResponse),
    )
    @iserror_aware
    async def cwms_get_value(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, _NAME_HINT],
        parameter: Annotated[str, _PARAMETER_HINT],
        window_hours: Annotated[
            int,
            "How far back to search for the most recent value, in hours.",
        ] = 24,
        unit: Annotated[Literal["EN", "SI"], _UNIT_HINT] = "EN",
        with_status: Annotated[
            bool,
            "Classify vs Location Levels (~1.5 s on SWT/FOSS; big offices often "
            "hit the 8 s cap: value kept, level_lookup_status 'timed_out').",
        ] = False,
        detail: Detail = Detail.SUMMARY,
    ) -> ValueWithContextResponse | ErrorRef:
        """Latest observation for a parameter at a place.

        Value-only and fast by default; `with_status=true` adds a
        threshold lookup (value kept on timeout) and carries `status_class`/
        `thresholds_active`. Auto-selects the canonical timeseries id.
        """
        raw = await _safe(
            values.get_value,
            office,
            name,
            parameter,
            window=timedelta(hours=window_hours),
            unit=unit,
            classify_against_levels=with_status,
            _repair_call=(
                "cwms_get_value",
                {
                    "office": office,
                    "name": name,
                    "parameter": parameter,
                    "window_hours": window_hours,
                    "unit": unit,
                    "with_status": with_status,
                    "detail": detail.value,
                },
            ),
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
            "title": "Windowed history",
        },
        output_schema=iserror_output_schema(HistoryResponse),
    )
    @iserror_aware
    async def cwms_get_history(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[str, _NAME_HINT],
        parameter: Annotated[str, _PARAMETER_HINT],
        begin_iso: Annotated[
            str,
            "Window start: "
            "RFC 3339 date-time with timezone (Z or ±HH:MM), e.g. 2026-05-17T00:00:00Z. "
            "Other unambiguous ISO 8601 spellings with an offset are accepted too; "
            "a date alone, or a time without an offset, is rejected.",
        ],
        end_iso: Annotated[
            str,
            "Window end: "
            "RFC 3339 date-time with timezone (Z or ±HH:MM), e.g. 2026-05-18T00:00:00Z. "
            "Other unambiguous ISO 8601 spellings with an offset are accepted too; "
            "a date alone, or a time without an offset, is rejected.",
        ],
        unit: Annotated[Literal["EN", "SI"], _UNIT_HINT] = "EN",
        rollup: Annotated[
            Rollup,
            "Downsample. 'raw' (default) returns every point and is capped at "
            "5,000 (30 days of 15-minute data is ~2,900 points, ~220 KB); for "
            "windows longer than about two days use 'hourly' or 'daily' — "
            "per-bucket min/max/mean/count in `buckets`, exempt from the raw "
            "cap (the upstream fetch cap can still apply; see `truncated`). "
            "`summary` is always present (null only when the window has no numeric "
            "observations).",
        ] = Rollup.RAW,
        detail: Detail = Detail.SUMMARY,
    ) -> HistoryResponse | ErrorRef:
        """Read observations across a bounded time window.

        For a value series over time; for latest value + status, use
        `cwms_get_value` instead (cheaper).

        Read `summary` for "how has X changed". For windows longer than about
        two days use `rollup='hourly'` or `'daily'`: `'raw'` returns every point
        up to a 5,000-point cap (30 days of 15-minute data is ~2,900 points,
        ~220 KB), while the rollups return per-bucket aggregates in `buckets`
        that are exempt from that cap. `summary` is always present (null only when
        the window has no numeric observations). If `truncated`, check `truncation_hint`: a
        local raw-point cap still covers the full window (rollup gives a
        complete summary), but an upstream fetch cap means `summary`/
        `buckets` cover only the fetched prefix — continue via `next_begin`
        until `truncated` is false.
        """
        try:
            begin = values.parse_rfc3339(begin_iso, field="begin_iso")
            end = values.parse_rfc3339(end_iso, field="end_iso")
            values.require_window(begin, end, begin_field="begin_iso", end_field="end_iso")
        except CwmsToolsError as err:
            return error_ref(err)
        raw = await _safe(
            values.get_history,
            office,
            name,
            parameter,
            begin=begin,
            end=end,
            unit=unit,
            rollup=rollup.value,
            _repair_call=(
                "cwms_get_history",
                {
                    "office": office,
                    "name": name,
                    "parameter": parameter,
                    "begin_iso": begin_iso,
                    "end_iso": end_iso,
                    "unit": unit,
                    "rollup": rollup.value,
                    "detail": detail.value,
                },
            ),
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
            "title": "Depth profile (whole string)",
        },
        output_schema=iserror_output_schema(ProfileResponse),
    )
    @iserror_aware
    async def cwms_get_profile(
        office: Annotated[str, _OFFICE_HINT],
        name: Annotated[
            str,
            "Parent 'string' case-sensitive catalog id (e.g. GWLW_S1) — NOT a single "
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
            _repair_call=(
                "cwms_get_profile",
                {
                    "office": office,
                    "name": name,
                    "parameter": parameter,
                    "window_hours": window_hours,
                    "unit": unit,
                    "detail": detail.value,
                },
            ),
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
            "title": "Publishers reporting a parameter",
        },
        output_schema=iserror_output_schema(PublishersForParameterResponse),
    )
    @iserror_aware
    async def cwms_publishers_for_parameter(
        parameter: Annotated[str, "Parameter code (e.g. Elev, Flow-In, Flow-Out, Stage)."],
        offices: Annotated[
            list[str] | None,
            "Office codes to index. Omit to answer from already-cached offices only "
            "— `coverage.complete` is false until every curated office is indexed; "
            "follow `repair` to index the rest.",
        ] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> PublishersForParameterResponse | ErrorRef:
        """List the publishers reporting a parameter, with explicit coverage.

        Indexes `offices`, or only already-cached offices when omitted —
        never fans out to every office. With `offices` omitted,
        `coverage.complete` is false until every curated office is indexed, and
        `repair` names the ones not yet indexed. Budget overflow lands in
        `coverage.offices_skipped_for_budget` with a `repair` hint back here.
        """
        raw = await _safe(
            publishers_index.publishers_for_parameter,
            parameter,
            offices=offices,
            _field_names={"office_id": "offices"},
            _repair_call=(
                "cwms_publishers_for_parameter",
                {
                    "parameter": parameter,
                    **({"offices": offices} if offices is not None else {}),
                    "detail": detail.value,
                },
            ),
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
        value=limit,
        reason="Pass limit=0 for no cap, or any non-negative integer.",
    )


async def _safe(
    fn,
    *args,
    _repair_call: tuple[str, dict[str, Any]] | None = None,
    _field_names: dict[str, str] | None = None,
    **kwargs,
) -> dict[str, Any] | ErrorRef:
    """Run a sync core function on the bounded executor; surface known errors structured.

    Returns the raw dict on success, or an `ErrorRef` (with fingerprint stamped)
    on any `CwmsToolsError`. Handlers check `isinstance(raw, ErrorRef)` and
    return it directly.

    `_repair_call`, when given, is `(tool_name, wire_format_args)` for THIS
    handler's own call (args as the agent would actually pass them,
    including `office`) — the single place a `ghost_office` failure gets a same-tool
    retry repair instead of core's old hardcoded `cwms_browse_region` switch
    (#69). Core can't build this itself: it doesn't know which surface/tool
    is calling, and `_safe`'s own `*args`/`**kwargs` are already core-format
    (`timedelta`/`datetime`/`BBox`, not `window_hours`/`begin_iso`/bbox
    floats), so this takes the handler's ORIGINAL wire-format args instead.

    `_field_names` is a per-tool override of `details.field` (producer name ->
    this tool's real argument name), applied before the generic
    `surface_field_name` translation (e.g. `office_id` -> `offices` for the
    tool whose argument is `offices`).
    """
    try:
        return await concurrency.run_sync(fn, *args, **kwargs)
    except CwmsToolsError as err:
        details = err.envelope.details
        if _field_names and details is not None and details.field in _field_names:
            details.field = _field_names[details.field]
        if _repair_call is not None:
            tool, call_args = _repair_call
            office_id = err.envelope.details.value if err.envelope.details else None
            listed = call_args.get("offices")
            if err.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(listed, list):
                # A tool whose office argument is a list (#183).
                err.envelope.repair = offices.ghost_offices_repair(
                    listed, tool=tool, args=call_args, key="offices"
                )
            elif err.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
                err.envelope.repair = offices.ghost_office_repair(
                    office_id, tool=tool, args=call_args
                )
            elif err.repair_intent is not None:
                err.envelope.repair = resolve_repair_intent(
                    err.repair_intent, tool=tool, args=call_args
                )
        return error_ref(err)


__all__ = [
    "error_ref",
    "iserror_aware",
    "register_place_tools",
    "register_publisher_tools",
    "register_value_tools",
    "stamp_envelope",
]
