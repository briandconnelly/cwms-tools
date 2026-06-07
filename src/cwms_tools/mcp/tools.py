"""MCP tool registrations.

Tools land here as `register_*` functions invoked from `mcp/server.py`. Each
function takes the FastMCP instance and adds its tools. Keeping the
registration out of the top-level `build_server` keeps that function
declarative and short as more tools land in later milestones.

Every successful tool response carries a `source.fingerprint` field (the
capability fingerprint at call time). Error responses use the structured
`{ok: false, error: {...}}` envelope.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from cwms_tools.core import concurrency, places, publishers_index, values
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.geo import BBox
from cwms_tools.core.models import (
    BrowseRegionResponse,
    DescribePlaceResponse,
    Detail,
    ErrorRef,
    HistoryResponse,
    ListParametersResponse,
    PublishersForParameterResponse,
    SearchPlacesResponse,
    SourceMeta,
    ValueWithContextResponse,
)
from cwms_tools.mcp.contract import canonical_fingerprint

if TYPE_CHECKING:
    from fastmcp import FastMCP


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


def register_place_tools(mcp: FastMCP) -> None:
    """Register the place-related tools on the FastMCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Search places by name",
        },
    )
    async def cwms_search_places(
        query: Annotated[str, "Name fragment to match, case-insensitive."],
        office: Annotated[
            str | list[str] | None,
            "USACE office code, or a list of office codes. Omit to fan out "
            "across offices already cached this session; pass an explicit "
            "list to widen. Unbounded discovery is intentionally avoided. "
            "New (uncached) offices in the list are capped per call; "
            "uncached overflow is returned under `offices_skipped_for_budget` "
            "with a repair hint pointing back at this tool with that list.",
        ] = None,
        parameter: Annotated[
            str | None,
            "Filter to locations publishing this parameter (e.g. Temp-Water, "
            "Elev, Flow-In). When set, locations that don't publish this "
            "parameter are dropped — except barren parents whose `data_at` "
            "siblings publish it (kept as a discovery hint). The response "
            "carries `nearby_non_matching_count` so the agent sees how much "
            "was filtered out.",
        ] = None,
        limit: Annotated[
            int,
            "Cap on the number of results (default 50). Broad queries like "
            "'Temp String' can match hundreds of rows; the cap keeps response "
            "size predictable. Pass 0 for no cap. When the cap kicks in the "
            "response carries `truncated: true` and `total_count`.",
        ] = places.DEFAULT_SEARCH_LIMIT,
        cursor: Annotated[
            str | None,
            "Opaque pagination cursor from a prior call's `next_cursor`. Pass it "
            "back verbatim to fetch the next page; omit it for the first page. "
            "On a stale cursor (changed query/filters or a shifted catalog) the "
            "tool returns the `invalid_cursor` error — restart without `cursor`.",
        ] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> SearchPlacesResponse | ErrorRef:
        """Resolve a CWMS place name to ranked location matches.

        Use for ambiguous name lookup. If you already have the canonical
        `office` and `name`, call `cwms_describe_place`,
        `cwms_list_parameters`, or `cwms_get_value` / `cwms_get_history`
        directly instead.

        Each result is enriched with parameter_count (0 means a ghost
        record with no published data), the parameters published at the
        location, the list of publishers active there, the most recent
        data timestamp, any other ids within ~100m of the same
        coordinates, and `data_at` — when a barren parent has a
        co-located data-bearing sibling, the sibling names land in
        `data_at` so the agent gets the repair hint without walking the
        co_located list. The `data_at` lookup falls back to the full
        office catalog when an in-result sibling does not match the
        query, so a parent like `FBLW` can still name its depth-tagged
        `FBLW_D1-*` temperature sensors. Data-bearing records sort
        first; ghosts are kept at the bottom of the list.
        """
        if limit < 0:
            return ErrorRef.from_error(_negative_limit_error(limit))
        effective_limit = None if limit == 0 else limit
        raw = await _safe(
            places.search_places,
            query,
            office=office,
            parameter=parameter,
            limit=effective_limit,
            cursor=cursor,
        )
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return SearchPlacesResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Describe a place",
        },
    )
    async def cwms_describe_place(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        name: Annotated[str, "Location id within the office (e.g. FTPK, FOSS)."],
        detail: Detail = Detail.SUMMARY,
    ) -> DescribePlaceResponse | ErrorRef:
        """Read everything about one place in a single call.

        Combines the location record, project metadata (when present),
        the parameters published at the location grouped by publisher,
        and the most recent data timestamp. Sets `partial: true` when
        any underlying lookup degrades (e.g. the upstream project record
        returns a format error); the `partial_reasons` field names the
        causes so the agent can decide whether to retry or proceed.
        """
        raw = await _safe(places.describe_place, office, name)
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_detail(raw, detail)
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
    )
    async def cwms_list_parameters(
        office: Annotated[str, "USACE office code."],
        name: Annotated[str, "Location id within the office."],
        detail: Detail = Detail.SUMMARY,
    ) -> ListParametersResponse | ErrorRef:
        """List the parameters published at a location, grouped by publisher.

        The cheapest probe for distinguishing data-bearing locations
        from ghost catalog records: a ghost returns `ts_count: 0` and an
        empty `by_publisher` list.
        """
        raw = await _safe(places.list_parameters, office, name)
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ListParametersResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Browse a region's catalog",
        },
    )
    async def cwms_browse_region(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        south: Annotated[float | None, "Bounding box south latitude in decimal degrees."] = None,
        west: Annotated[float | None, "Bounding box west longitude in decimal degrees."] = None,
        north: Annotated[float | None, "Bounding box north latitude in decimal degrees."] = None,
        east: Annotated[float | None, "Bounding box east longitude in decimal degrees."] = None,
        state: Annotated[str | None, "Two-letter US state code (e.g. MT, OK)."] = None,
        limit: Annotated[
            int,
            "Cap on the number of results (default 50). A no-filter browse of a "
            "large office can return thousands of rows; the cap keeps the response "
            "bounded. Pass 0 for no cap. When the cap kicks in the response carries "
            "`truncated: true`, `total_count`, and `truncation_hint`. Data-bearing "
            "rows sort ahead of ghosts so a capped browse keeps the useful records.",
        ] = places.DEFAULT_BROWSE_LIMIT,
        cursor: Annotated[
            str | None,
            "Opaque pagination cursor from a prior call's `next_cursor`. Pass it "
            "back verbatim to fetch the next page; omit it for the first page. "
            "On a stale cursor (changed query/filters or a shifted catalog) the "
            "tool returns the `invalid_cursor` error — restart without `cursor`.",
        ] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> BrowseRegionResponse | ErrorRef:
        """Browse the locations published by one office, optionally filtered.

        Returns the same enriched per-place records as `cwms_search_places`
        (including `parameters` and the `data_at` repair hint), with
        `result_count`, `ghost_count`, and `total_count` totals at the top. The
        bounding-box filter requires all four corners or none.
        """
        bbox: BBox | None = None
        provided = [v for v in (south, west, north, east) if v is not None]
        if len(provided) not in {0, 4}:
            return ErrorRef.from_error(
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
            return ErrorRef.from_error(_negative_limit_error(limit))
        effective_limit = None if limit == 0 else limit
        raw = await _safe(
            places.browse_region,
            office=office,
            bbox=bbox,
            state=state,
            limit=effective_limit,
            cursor=cursor,
        )
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_detail(raw, detail)
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
    )
    async def cwms_get_value(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        name: Annotated[
            str,
            "CWMS location name/id within the office (e.g. FTPK, FOSS, "
            "or a depth-tagged sensor like UBLW_S1-D21,0ft).",
        ],
        parameter: Annotated[
            str,
            "Parameter code. Common examples: Temp-Water, Stage, Elev, Flow-In, "
            "Flow-Out, Precip, Conc-DO, Volt-Battery. Case-sensitive. See "
            "`cwms_list_parameters` on a known location for the full set.",
        ],
        window_hours: Annotated[
            int,
            "How far back to search for the most recent value, in hours.",
        ] = 24,
        unit: Annotated[
            Literal["EN", "SI"],
            "Unit system: 'EN' for English (ft, cfs) or 'SI' for metric (m, cms).",
        ] = "EN",
        with_status: Annotated[
            bool,
            "Classify the observation against applicable CWMS Location Levels. "
            "OFF by default — the levels lookup is reliably slow (the 8 s "
            "budget often expires on cold cache). The response always carries "
            "`level_lookup_status` (skipped, computed, timed_out, unavailable) "
            "so callers can see what happened.",
        ] = False,
        detail: Detail = Detail.SUMMARY,
    ) -> ValueWithContextResponse | ErrorRef:
        """Latest observation for a parameter at a place.

        Value-only and fast by default. Set `with_status=true` to also
        classify against applicable thresholds; that path is slow and
        often times out — agents on a tight budget should leave it off
        and follow up with a separate classification step if needed.

        Auto-selects the canonical (best publisher) timeseries id at the
        location. When classification ran successfully the response
        carries `status_class` (nominal, watch, action, flood, or
        unknown) and `thresholds_active` with the signed delta from the
        observation to each threshold.
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
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_value_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ValueWithContextResponse.model_validate(shaped)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Windowed history",
        },
    )
    async def cwms_get_history(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        name: Annotated[str, "CWMS location name/id within the office (e.g. FTPK, FOSS)."],
        parameter: Annotated[
            str,
            "Parameter code. Common examples: Temp-Water, Stage, Elev, Flow-In, "
            "Flow-Out, Precip, Conc-DO, Volt-Battery. Case-sensitive. See "
            "`cwms_list_parameters` on a known location for the full set.",
        ],
        begin_iso: Annotated[
            str, "Window start as an RFC3339 timestamp (e.g. 2026-05-17T00:00:00Z)."
        ],
        end_iso: Annotated[
            str,
            "Window end as an RFC3339 timestamp (e.g. 2026-05-18T00:00:00Z).",
        ],
        unit: Annotated[
            Literal["EN", "SI"],
            "Unit system: 'EN' for English (ft, cfs) or 'SI' for metric (m, cms).",
        ] = "EN",
        detail: Detail = Detail.SUMMARY,
    ) -> HistoryResponse | ErrorRef:
        """Read raw observations across a bounded time window.

        Use for a series of values over time. For the latest value plus
        threshold-derived status, call `cwms_get_value` instead — it is
        cheaper and includes the classification this tool does not.
        Returns the values array (timestamp + value, plus quality codes
        at `detail=full`) along with the resolved canonical timeseries
        id. `truncated: true` with a `truncation_hint` is set when the
        upstream page cap (300,000 points) clipped the requested window.
        """
        try:
            begin = datetime.fromisoformat(begin_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            return ErrorRef.from_error(
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
            return ErrorRef.from_error(
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
        )
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_history_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return HistoryResponse.model_validate(shaped)


def register_publisher_tools(mcp: FastMCP) -> None:
    """Register the publisher-related helper tools on the FastMCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Publishers reporting a parameter",
        },
    )
    async def cwms_publishers_for_parameter(
        parameter: Annotated[str, "Parameter code (e.g. Elev, Flow-In, Flow-Out, Stage)."],
        offices: Annotated[
            list[str] | None,
            "Limit the index to these office codes. If omitted, only "
            "offices already in cache are scanned; never expands to every "
            "office implicitly.",
        ] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> PublishersForParameterResponse | ErrorRef:
        """List the publishers reporting a parameter, with explicit coverage.

        Indexes the offices in `offices`; when `offices` is omitted, only
        offices already in the local cache are scanned — this tool never
        implicitly fans out to every office. A per-call budget caps how
        many uncached offices it fetches, and any beyond the budget land
        in `coverage.offices_skipped_for_budget` with a `repair` hint
        that points back at this tool with that list, so the caller can
        continue the index in deterministic chunks.
        """
        raw = await _safe(
            publishers_index.publishers_for_parameter,
            parameter,
            offices=offices,
        )
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_publishers_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return PublishersForParameterResponse.model_validate(shaped)


# --------------------------------------------------------------------------
# Detail toggle helpers
# --------------------------------------------------------------------------


def _shape_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Apply the `detail` toggle to a place-tool response."""
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    if "location" in pruned and isinstance(pruned["location"], dict):
        loc = pruned["location"]
        pruned["location"] = {
            k: loc.get(k)
            for k in (
                "office-id",
                "name",
                "location-kind",
                "latitude",
                "longitude",
                "public-name",
                "long-name",
                "horizontal-datum",
                "state-initial",
                "nearest-city",
                "timezone-name",
            )
            if k in loc
        }
    if "results" in pruned and isinstance(pruned["results"], list):
        pruned["results"] = [
            {k: v for k, v in r.items() if k != "raw"}
            for r in pruned["results"]
            if isinstance(r, dict)
        ]
    return pruned


def _shape_value_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    if isinstance(pruned.get("thresholds_active"), list):
        pruned["thresholds_active"] = [
            {k: v for k, v in t.items() if k not in {"level_id", "source_workaround"}}
            for t in pruned["thresholds_active"]
        ]
    return pruned


def _shape_history_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    if isinstance(pruned.get("values"), list):
        pruned["values"] = [
            {k: v for k, v in row.items() if k != "quality"} for row in pruned["values"]
        ]
    return pruned


def _shape_publishers_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    pruned.pop("_observed_publishers_by_office", None)
    return pruned


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


async def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    """Run a sync core function on the bounded executor; surface known errors structured.

    Pre-`_safe` validation branches (e.g. partial-bbox, bad RFC3339) build the same
    shape via `ErrorRef.from_error(...)`, so manual validation errors land with the
    full envelope (`request_id`, `offending_value`, `hint`, `repair`, source).
    """
    try:
        return await concurrency.run_sync(fn, *args, **kwargs)
    except CwmsToolsError as err:
        return {"ok": False, "error": err.envelope.model_dump(mode="json")}


__all__ = [
    "register_place_tools",
    "register_publisher_tools",
    "register_value_tools",
]
