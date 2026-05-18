"""MCP tool registrations.

Tools land here as `register_*` functions invoked from `mcp/server.py`. Each
function takes the FastMCP instance and adds its tools. Keeping the
registration out of the top-level `build_server` keeps that function
declarative and short as more tools land in later milestones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from cwms_tools.core import concurrency, places

if TYPE_CHECKING:
    from fastmcp import FastMCP
from cwms_tools.core.errors import CwmsToolsError
from cwms_tools.core.geo import BBox
from cwms_tools.core.models import Detail


def register_place_tools(mcp: FastMCP) -> None:
    """Register the four §M4 place tools on the FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Search places by name"},
    )
    async def cwms_search_places(
        query: Annotated[str, "Name fragment to match (case-insensitive)"],
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT, MVS)"],
        detail: Detail = Detail.SUMMARY,
    ) -> dict[str, Any]:
        """Resolve a place name in one call.

        Returns ghost-filtered, co-located, ranked location matches with
        publisher + parameter fingerprints inlined. Data-bearing records
        sort first; ghosts (parameter_count=0) are kept but at the bottom.
        Matches §9.1 steps 1-2 of cwms-overview.md.
        """
        return _shape_detail(await _safe(places.search_places, query, office=office), detail)

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Describe a place"},
    )
    async def cwms_describe_place(
        office: Annotated[str, "USACE office code (e.g. NWDM)"],
        name: Annotated[str, "Location id within the office"],
        detail: Detail = Detail.SUMMARY,
    ) -> dict[str, Any]:
        """Full Location + Project + parameter set + publisher fingerprint + freshness.

        Carries `partial: true` and `partial_reasons` when any sub-call
        falls back (e.g. get_project format-error). §9.9 / §9.6.
        """
        return _shape_detail(await _safe(places.describe_place, office, name), detail)

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "List parameters at a place"},
    )
    async def cwms_list_parameters(
        office: Annotated[str, "USACE office code"],
        name: Annotated[str, "Location id within the office"],
        detail: Detail = Detail.SUMMARY,
    ) -> dict[str, Any]:
        """Parameters published at the location, grouped by publisher.

        Use this as the cheapest ghost-detection probe: returns ts_count=0
        and an empty `by_publisher` for ghost records. §9.6 reduced.
        """
        return _shape_detail(await _safe(places.list_parameters, office, name), detail)

    @mcp.tool(
        annotations={"readOnlyHint": True, "title": "Browse a region's catalog"},
    )
    async def cwms_browse_region(
        office: Annotated[str, "USACE office code (e.g. NWDM, SWT)"],
        south: Annotated[float | None, "Bounding-box south latitude"] = None,
        west: Annotated[float | None, "Bounding-box west longitude"] = None,
        north: Annotated[float | None, "Bounding-box north latitude"] = None,
        east: Annotated[float | None, "Bounding-box east longitude"] = None,
        state: Annotated[str | None, "Two-letter state code filter"] = None,
        detail: Detail = Detail.SUMMARY,
    ) -> dict[str, Any]:
        """Enriched catalog browse filtered by office, bbox, or state.

        All four bbox corners must be set together (or none of them). §9.7.
        """
        bbox: BBox | None = None
        provided = [v for v in (south, west, north, east) if v is not None]
        if len(provided) not in {0, 4}:
            return {
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
        if south is not None and west is not None and north is not None and east is not None:
            bbox = BBox(south=south, west=west, north=north, east=east)
        return _shape_detail(
            await _safe(places.browse_region, office=office, bbox=bbox, state=state),
            detail,
        )


def _shape_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Apply the `detail` toggle to a tool response.

    Density only, never shape (agent-friendly-mcp §8). Summary mode strips
    the heavy upstream Location DTO and per-row `raw` payloads from
    catalog-browse responses. Error envelopes pass through unchanged.
    """
    if payload.get("ok") is False:
        return payload
    if detail is Detail.FULL:
        return payload
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


async def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    """Run a sync core function on the bounded executor; surface known errors structured."""
    try:
        return await concurrency.run_sync(fn, *args, **kwargs)
    except CwmsToolsError as err:
        return {"ok": False, "error": err.envelope.model_dump(mode="json")}


__all__ = ["register_place_tools"]
