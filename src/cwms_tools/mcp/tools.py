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
from typing import TYPE_CHECKING, Annotated, Any

from cwms_tools.core import concurrency, fingerprint, places, publishers_index, values
from cwms_tools.core.errors import CwmsToolsError
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
from cwms_tools.mcp.resources import RESOURCE_INVENTORY, TOOL_INVENTORY

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _source(workaround: str | None = None) -> SourceMeta:
    """Build the per-response provenance, including the capability fingerprint."""
    fp = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=RESOURCE_INVENTORY,
    )
    return SourceMeta(fingerprint=fp, workaround=workaround)


def register_place_tools(mcp: FastMCP) -> None:
    """Register the place-related tools on the FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Search places by name"},
    )
    async def cwms_search_places(
        query: Annotated[str, "Name fragment to match, case-insensitive."],
        office: Annotated[
            str,
            "USACE office code (e.g. NWDM, SWT, MVS). Required because catalog "
            "search is per-office.",
        ],
        detail: Detail = Detail.SUMMARY,
    ) -> SearchPlacesResponse | ErrorRef:
        """Resolve a CWMS place name to ranked location matches.

        Use for ambiguous name lookup within one office. If you already
        have the canonical `office` and `name`, call `cwms_describe_place`,
        `cwms_list_parameters`, or `cwms_get_value` / `cwms_get_history`
        directly instead.

        Each result is enriched with parameter_count (0 means a ghost
        record with no published data), the list of publishers active at
        the location, the most recent data timestamp, and any other ids
        within ~100m of the same coordinates. Data-bearing records sort
        first; ghosts are kept at the bottom of the list.
        """
        raw = await _safe(places.search_places, query, office=office)
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return SearchPlacesResponse.model_validate(shaped)

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Describe a place"},
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
        shaped["source"] = _source(workaround=workaround).model_dump(mode="json")
        return DescribePlaceResponse.model_validate(shaped)

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "List parameters at a place"},
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
        annotations={"readOnlyHint": True, "title": "Browse a region's catalog"},
    )
    async def cwms_browse_region(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        south: Annotated[float | None, "Bounding box south latitude in decimal degrees."] = None,
        west: Annotated[float | None, "Bounding box west longitude in decimal degrees."] = None,
        north: Annotated[float | None, "Bounding box north latitude in decimal degrees."] = None,
        east: Annotated[float | None, "Bounding box east longitude in decimal degrees."] = None,
        state: Annotated[str | None, "Two-letter US state code (e.g. MT, OK)."] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> BrowseRegionResponse | ErrorRef:
        """Browse the locations published by one office, optionally filtered.

        Returns the same enriched per-place records as `cwms_search_places`,
        with `result_count` and `ghost_count` totals at the top. The
        bounding-box filter requires all four corners or none.
        """
        bbox: BBox | None = None
        provided = [v for v in (south, west, north, east) if v is not None]
        if len(provided) not in {0, 4}:
            return ErrorRef.model_validate(
                {
                    "ok": False,
                    "error": {
                        "code": "usage_error",
                        "message": (
                            "When specifying a bounding box, all four of south, "
                            "west, north, east must be provided."
                        ),
                        "field": "bbox",
                    },
                }
            )
        if south is not None and west is not None and north is not None and east is not None:
            bbox = BBox(south=south, west=west, north=north, east=east)
        raw = await _safe(places.browse_region, office=office, bbox=bbox, state=state)
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return BrowseRegionResponse.model_validate(shaped)


def register_value_tools(mcp: FastMCP) -> None:
    """Register the value-related tools on the FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Current value with status context"},
    )
    async def cwms_get_value(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        name: Annotated[str, "CWMS location name/id within the office (e.g. FTPK, FOSS)."],
        parameter: Annotated[
            str, "Parameter code (e.g. Elev, Flow-In, Flow-Out, Stage, Temp-Water)."
        ],
        window_hours: Annotated[
            int,
            "How far back to search for the most recent value, in hours.",
        ] = 24,
        unit: Annotated[
            str, "Unit system: 'EN' for English (ft, cfs) or 'SI' for metric (m, cms)."
        ] = "EN",
        detail: Detail = Detail.SUMMARY,
    ) -> ValueWithContextResponse | ErrorRef:
        """Latest observation for a parameter at a place, with inline status.

        Use for a single point-in-time reading. For a windowed history,
        call `cwms_get_history`. Auto-selects the canonical (best
        publisher) timeseries id at the location. The response includes
        `status_class` (nominal, watch, action, flood, or unknown)
        computed against the applicable thresholds for the parameter,
        plus the list of active thresholds with the signed delta from
        the observation to each.
        """
        raw = await _safe(
            values.get_value,
            office,
            name,
            parameter,
            window=timedelta(hours=window_hours),
            unit=unit,
        )
        if raw.get("ok") is False:
            return ErrorRef.model_validate(raw)
        shaped = _shape_value_detail(raw, detail)
        shaped["source"] = _source().model_dump(mode="json")
        return ValueWithContextResponse.model_validate(shaped)

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Windowed history"},
    )
    async def cwms_get_history(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)."],
        name: Annotated[str, "CWMS location name/id within the office (e.g. FTPK, FOSS)."],
        parameter: Annotated[
            str, "Parameter code (e.g. Elev, Flow-In, Flow-Out, Stage, Temp-Water)."
        ],
        begin_iso: Annotated[
            str, "Window start as an RFC3339 timestamp (e.g. 2026-05-17T00:00:00Z)."
        ],
        end_iso: Annotated[
            str,
            "Window end as an RFC3339 timestamp (e.g. 2026-05-18T00:00:00Z).",
        ],
        unit: Annotated[
            str, "Unit system: 'EN' for English (ft, cfs) or 'SI' for metric (m, cms)."
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
            end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            return ErrorRef.model_validate(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_field",
                        "message": f"Could not parse begin/end as RFC3339 datetimes: {exc}",
                        "field": "begin_iso/end_iso",
                    },
                }
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
        annotations={"readOnlyHint": True, "title": "Publishers reporting a parameter"},
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


async def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    """Run a sync core function on the bounded executor; surface known errors structured."""
    try:
        return await concurrency.run_sync(fn, *args, **kwargs)
    except CwmsToolsError as err:
        return {"ok": False, "error": err.envelope.model_dump(mode="json")}


__all__ = [
    "register_place_tools",
    "register_publisher_tools",
    "register_value_tools",
]
