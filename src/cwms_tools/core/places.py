"""Task-completing logic for the four place tools (M4).

This module is the layer between the MCP/CLI adapters and the per-resource
wrappers in `core.catalog`, `core.locations`, `core.projects`, and
`core.publishers`. It produces the response shapes declared in
`core.models` — task-response models, not raw upstream DTOs.
"""

from __future__ import annotations

from typing import Any

from cwms_tools.core import catalog, locations, projects, publishers
from cwms_tools.core.geo import BBox, GeoPoint, filter_by_bbox


def search_places(
    query: str,
    *,
    office: str,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_search_places` — name resolution with enrichment.

    Returns the SearchPlacesResult shape: ghost-filtered + co-located, ranked
    so data-bearing records come first. Ghosts (parameter_count == 0) are
    kept but sort to the bottom so the agent can still see them. Each
    barren result carries a `data_at` repair hint listing co-located
    siblings that DO publish data (the depth-tagged child case from
    Lake Washington / UBLW_S1 — the parent is empty but `UBLW_S1-D21,0ft`
    holds the actual sensors).
    """
    enriched = locations.search(office, query, use_cache=use_cache)
    enriched.sort(key=lambda r: (-r["parameter_count"], r["name"]))
    by_name = {r["name"]: r for r in enriched}
    return {
        "query": query,
        "office": office,
        "results": [
            {
                "office_id": r["office_id"],
                "name": r["name"],
                "public_name": r.get("public_name"),
                "location_kind": r.get("location_kind"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "parameter_count": r["parameter_count"],
                "publishers": r["publishers"],
                "last_data_timestamp": r.get("last_data_timestamp"),
                "co_located": r.get("co_located", []),
                "data_at": _data_at_hint(r, by_name),
            }
            for r in enriched
        ],
    }


def describe_place(
    office: str,
    name: str,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_describe_place` — full Location + Project + publisher fingerprint.

    Combines up to 4 upstream calls (get_location, get_project with fallback,
    get_timeseries_catalog, freshness derivation). When any sub-call fails
    in a way we can recover from, the response carries `partial: true` and
    `partial_reasons: [...]` so the agent sees the truncation.
    """
    location = locations.get_one(office, name, use_cache=use_cache)
    project_resp = projects.get_one(office, name, use_cache=use_cache)
    ts_ids = catalog.ts_ids_for_location(office, name, use_cache=use_cache)
    param_counts = publishers.parameter_counts(ts_ids)
    pub_facts = publishers.aggregate_publishers(ts_ids)
    freshness = catalog.freshness_for_location(office, name, use_cache=use_cache)

    return {
        "office_id": office,
        "name": name,
        "location": location,
        "project": project_resp.get("project_metadata"),
        "partial": project_resp.get("partial", False),
        "partial_reasons": project_resp.get("partial_reasons", []),
        "parameters": sorted(param_counts.keys()),
        "parameter_count": len(param_counts),
        "publishers": [
            {
                "publisher": f.publisher,
                "rank": f.rank,
                "ts_count": f.ts_count,
                "parameters": list(f.parameters),
            }
            for f in pub_facts
        ],
        "ts_ids": ts_ids,
        "last_data_timestamp": freshness,
        "source_workaround": project_resp.get("source_workaround"),
        "upstream_status": project_resp.get("upstream_status"),
    }


def list_parameters(
    office: str,
    name: str,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_list_parameters` — parameters at a location, grouped by publisher.

    When the location is barren (`ts_count == 0`), the response carries a
    top-level `data_at` field listing co-located siblings that DO publish
    data — this is the common Lake Washington / UBLW_S1 case where the
    parent has no ts ids but a depth-tagged child does. When the location
    is data-bearing, `data_at` is null (no repair needed).
    """
    ts_ids = catalog.ts_ids_for_location(office, name, use_cache=use_cache)
    pub_facts = publishers.aggregate_publishers(ts_ids)
    freshness = catalog.freshness_for_location(office, name, use_cache=use_cache)
    return {
        "office_id": office,
        "name": name,
        "ts_count": len(ts_ids),
        "by_publisher": [
            {
                "publisher": f.publisher,
                "rank": f.rank,
                "parameters": list(f.parameters),
                "ts_count": f.ts_count,
            }
            for f in pub_facts
        ],
        "all_parameters": sorted(publishers.parameter_counts(ts_ids).keys()),
        "last_data_timestamp": freshness,
        "data_at": _data_at_for_location(office, name, use_cache=use_cache) if not ts_ids else None,
    }


def _data_at_hint(row: dict[str, Any], by_name: dict[str, dict[str, Any]]) -> list[str]:
    """Names of co-located siblings (in the same enriched result set) that
    publish data, returned only when this row itself is barren.

    Empty when the row has data or when no data-bearing co-located sibling
    exists. Stable ordering so snapshot tests don't churn.
    """
    if row.get("parameter_count", 0) > 0:
        return []
    siblings = row.get("co_located") or []
    if not siblings:
        return []
    return sorted(s for s in siblings if (by_name.get(s) or {}).get("parameter_count", 0) > 0)


def _data_at_for_location(
    office: str,
    name: str,
    *,
    use_cache: bool,
) -> list[str]:
    """Find data-bearing co-located siblings for a single named location.

    Used by `list_parameters` when the requested location is barren. Pulls
    the enriched office catalog (cached) and reads the co_located list for
    `name`, then filters to siblings with `parameter_count > 0`. Returns
    an empty list when no sibling has data or when the location isn't in
    the office catalog at all.
    """
    enriched = catalog.enrich_locations(office, use_cache=use_cache)
    target = next((r for r in enriched if r["name"] == name), None)
    if target is None:
        return []
    siblings = target.get("co_located") or []
    if not siblings:
        return []
    by_name = {r["name"]: r for r in enriched}
    return sorted(s for s in siblings if (by_name.get(s) or {}).get("parameter_count", 0) > 0)


def browse_region(
    *,
    office: str,
    bbox: BBox | None = None,
    state: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_browse_region` — enriched catalog filtered by office, bbox, or state.

    Filtering happens client-side because CDA doesn't expose bbox or radius
    queries. Co-location is computed across the office's full catalog before
    bbox filtering so siblings outside the bbox can still be flagged.
    """
    enriched = catalog.enrich_locations(office, use_cache=use_cache)
    rows = enriched

    if state:
        target = state.upper()

        # CDA returns the two-letter state code as `state` on the catalog row
        # and as `state-initial` on the single-location response. Check both
        # so the filter works regardless of which shape arrived.
        def _matches_state(r: dict[str, Any]) -> bool:
            s = _row_state(r)
            return s is not None and s.upper() == target

        rows = [r for r in rows if _matches_state(r)]

    if bbox is not None:
        geopoints = [
            GeoPoint(office_id=r["office_id"], name=r["name"], latitude=lat, longitude=lon)
            for r in rows
            if isinstance((lat := r.get("latitude")), (int, float))
            and isinstance((lon := r.get("longitude")), (int, float))
        ]
        in_bbox = {(g.office_id, g.name) for g in filter_by_bbox(geopoints, bbox)}
        rows = [r for r in rows if (r["office_id"], r["name"]) in in_bbox]

    # Drop the verbose `raw` field for region browse responses — agents asking
    # for a region overview don't need every per-row DTO. They can fetch
    # cwms_describe_place for any specific hit.
    return {
        "office": office,
        "bbox": _bbox_to_dict(bbox),
        "state": state,
        "result_count": len(rows),
        "ghost_count": sum(1 for r in rows if r["parameter_count"] == 0),
        "results": [
            {
                "office_id": r["office_id"],
                "name": r["name"],
                "public_name": r.get("public_name"),
                "location_kind": r.get("location_kind"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "parameter_count": r["parameter_count"],
                "publishers": r["publishers"],
                "last_data_timestamp": r.get("last_data_timestamp"),
                "co_located": r.get("co_located", []),
            }
            for r in rows
        ],
    }


def _bbox_to_dict(bbox: BBox | None) -> dict[str, float] | None:
    if bbox is None:
        return None
    return {"south": bbox.south, "west": bbox.west, "north": bbox.north, "east": bbox.east}


def _row_state(row: dict[str, Any]) -> str | None:
    """Pluck a two-letter state code from a catalog row, tolerating either shape."""
    raw = row.get("raw") or {}
    for key in ("state-initial", "state"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            return v
    return None


__all__ = ["browse_region", "describe_place", "list_parameters", "search_places"]
