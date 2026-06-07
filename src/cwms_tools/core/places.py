"""Task-completing logic for the four place tools (M4).

This module is the layer between the MCP/CLI adapters and the per-resource
wrappers in `core.catalog`, `core.locations`, `core.projects`, and
`core.publishers`. It produces the response shapes declared in
`core.models` — task-response models, not raw upstream DTOs.
"""

from __future__ import annotations

import math
from typing import Any

from cwms_tools.core import catalog, locations, offices, pagination, projects, publishers
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.concurrency import MAX_WORKERS
from cwms_tools.core.geo import BBox, GeoPoint, filter_by_bbox
from cwms_tools.core.session import current_config

DEFAULT_SEARCH_LIMIT: int = 50
DEFAULT_BROWSE_LIMIT: int = 50


def _fanout_budget() -> int:
    """How many uncached offices we will fetch per `search_places` call.

    Mirrors `core/publishers_index._budget()`. Capping new fetches keeps
    cold-cache fanout bounded so a single search doesn't trigger ~68
    upstream calls.
    """
    return max(1, math.ceil(MAX_WORKERS / 2))


def _normalize_office_arg(office: str | list[str] | None) -> list[str] | None:
    """Return the explicit office list, or None to mean "use cached scope"."""
    if office is None:
        return None
    if isinstance(office, str):
        return [office]
    seen: set[str] = set()
    out: list[str] = []
    for o in office:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def search_places(
    query: str,
    *,
    office: str | list[str] | None = None,
    parameter: str | None = None,
    limit: int | None = DEFAULT_SEARCH_LIMIT,
    cursor: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_search_places` — name resolution with enrichment.

    Returns the SearchPlacesResult shape: ghost-filtered + co-located, ranked
    so data-bearing records come first. Each barren result carries a
    `data_at` repair hint listing co-located siblings that DO publish data
    (the depth-tagged child case from Lake Washington / UBLW_S1 — the parent
    is empty but `UBLW_S1-D21,0ft` holds the actual sensors).

    `office`: a single office code, an explicit list of office codes, or
    `None` to use the cached scope. `None` fans out only across offices
    whose unfiltered locations catalog is already cached this session;
    unbounded discovery is intentionally avoided. Pass an explicit list to
    widen. New (uncached) offices in the list are capped per call by
    `_fanout_budget()`; offices not fetched are listed under
    `offices_skipped_for_budget` with a repair hint pointing back at this
    tool with that list as the next `office` argument.

    `parameter`: optional CWMS parameter code (e.g. `Temp-Water`). When
    set, data-bearing rows that do not publish this parameter are dropped
    from `results`; barren rows are kept only when their `data_at`
    siblings publish the parameter (the depth-sensor repair case). The
    response carries `nearby_non_matching_count` so the agent sees how
    much was filtered without paying for the filtered rows themselves.

    `limit` caps the number of results returned (default 50). Broad
    searches can return hundreds of rows on a big office; the cap
    prevents flooding the caller. Set `limit=None` (or `limit=0` on the
    CLI) to return every match. When the cap kicks in, the response
    carries `truncated: true` and `total_count`.

    Pagination: when the result set exceeds `limit`, the response sets
    `has_more: true` and returns an opaque `next_cursor`. Pass that value
    back as `cursor` to fetch the next page; the cursor locks the searched
    office set and the query/parameter, so continuation is deterministic.
    A stale cursor (changed query/parameter, or a catalog that shifted)
    raises an `invalid_cursor` error — restart without `cursor`. `limit=None`
    (or `limit=0` on the CLI) returns all results and never paginates.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer or None")
    if limit == 0:
        limit = None  # 0 means "no cap"; normalize so pagination math is well-defined

    req = pagination.request_hash({"q": query, "parameter": parameter})
    decoded: dict[str, Any] | None = None
    offset = 0
    if cursor is not None:
        decoded = pagination.decode_cursor(cursor)
        # Cheap checks BEFORE upstream fan-out: kind, request hash, offset shape,
        # and a bounded/typed office set (a forged cursor must not widen the fan-out).
        offset = pagination.validate_continuation(decoded, kind="search_places", req=req)
        offices_searched = pagination.coerce_offices(decoded)
        offices_skipped: list[str] = []
        partial_reasons: list[str] = []
    else:
        requested = _normalize_office_arg(office)
        if requested is None:
            requested = offices.cached_offices_for_locations()
        offices_searched, offices_skipped, partial_reasons = _run_fanout(requested)

    enriched, filtered_out = _apply_parameter_filter(
        _gather_enriched(offices_searched, query, use_cache=use_cache),
        parameter,
        use_cache=use_cache,
    )
    enriched.sort(key=lambda r: (-r["parameter_count"], r["office_id"], r["name"]))
    total_count = len(enriched)
    if decoded is not None:
        pagination.ensure_total(decoded, total=total_count)  # catalog-shift guard

    next_cursor: str | None = None
    if limit is None:
        page = enriched[offset:]
        has_more = False
    else:
        next_offset = offset + limit
        page = enriched[offset:next_offset]
        has_more = next_offset < total_count
        if has_more:
            next_cursor = pagination.encode_cursor(
                {
                    "v": pagination.CURSOR_VERSION,
                    "kind": "search_places",
                    "off": next_offset,
                    "req": req,
                    "offices": offices_searched,
                    "total": total_count,
                }
            )

    results = [
        {
            "office_id": r["office_id"],
            "name": r["name"],
            "public_name": r.get("public_name"),
            "location_kind": r.get("location_kind"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "parameter_count": r["parameter_count"],
            "parameters": r.get("parameters", []),
            "publishers": r["publishers"],
            "last_data_timestamp": r.get("last_data_timestamp"),
            "co_located": r.get("co_located", []),
            "data_at": r.get("data_at", []),
        }
        for r in page
    ]

    response: dict[str, Any] = {
        "query": query,
        "office": office,
        "offices_searched": offices_searched,
        "offices_skipped_for_budget": offices_skipped,
        "results": results,
        "total_count": total_count,
        "truncated": has_more,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": limit,
    }
    if parameter is not None:
        response["parameter"] = parameter
        response["nearby_non_matching_count"] = filtered_out
    if partial_reasons:
        response["partial"] = True
        response["partial_reasons"] = partial_reasons
    return response


def _run_fanout(requested: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Decide which offices to search; return (searched, skipped, reasons).

    Mirrors `publishers_index.publishers_for_parameter`: cached offices
    are always allowed; uncached offices consume the per-call budget.
    Offices that error during the actual search are flagged via
    `partial_reasons` in the parent response.
    """
    budget = _fanout_budget()
    searched: list[str] = []
    skipped: list[str] = []
    reasons: list[str] = []
    for office in requested:
        cached = _location_catalog_cached(office)
        if cached or budget > 0:
            searched.append(office)
            if not cached:
                budget -= 1
        else:
            skipped.append(office)
    if not requested:
        reasons.append("no_offices_in_scope: omit `office` or pass an explicit list to widen")
    return searched, skipped, reasons


def _location_catalog_cached(office: str) -> bool:
    """Cheap probe: is `office`'s unfiltered locations catalog cached?"""

    cache = get_cache()
    cfg = current_config()
    key = build_cache_key("location_catalog", office, "", api_root=cfg.api_root)
    return cache.get(key) is not None


def _gather_enriched(
    office_ids: list[str],
    query: str,
    *,
    use_cache: bool,
) -> list[dict[str, Any]]:
    """Run filtered enrichment per office and merge."""
    merged: list[dict[str, Any]] = []
    for office_id in office_ids:
        try:
            rows = locations.search(office_id, query, use_cache=use_cache)
        except Exception:
            continue
        merged.extend(rows)
    return merged


def _apply_parameter_filter(
    enriched: list[dict[str, Any]],
    parameter: str | None,
    *,
    use_cache: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Compute `data_at` per row and apply the optional parameter filter.

    Returns `(kept_rows, dropped_count)`. `dropped_count` is 0 when no
    parameter filter is set.

    When `parameter` is set, also pulls in *co-located siblings publishing
    the parameter* from the broader (unfiltered) office catalog, even
    when those siblings did not literally match the search query. This
    is what makes the Fremont Bridge probe succeed: `cwms_search_places(
    "Fremont Bridge", parameter="Temp-Water")` surfaces `FBLW_D1-D5,0ft`
    even though that id contains no "Fremont Bridge" string.
    """
    if not enriched:
        return [], 0

    by_office: dict[str, dict[str, dict[str, Any]]] = {}
    for r in enriched:
        by_office.setdefault(r["office_id"], {})[r["name"]] = r
    broader_by_office: dict[str, dict[str, dict[str, Any]]] = {}

    def broader(office_id: str) -> dict[str, dict[str, Any]]:
        cached = broader_by_office.get(office_id)
        if cached is not None:
            return cached
        try:
            rows = catalog.enrich_locations(office_id, use_cache=use_cache)
        except Exception:
            rows = []
        index = {r["name"]: r for r in rows}
        broader_by_office[office_id] = index
        return index

    def annotate_data_at(row: dict[str, Any]) -> None:
        """Set `row["data_at"]` from the broader catalog when this row is barren."""
        office_id = row["office_id"]
        in_office_by_name = by_office[office_id]
        siblings = row.get("co_located") or []
        data_at = _sibling_data_at(siblings, in_office_by_name, parameter)
        if not data_at and row.get("parameter_count", 0) == 0:
            broader_by_name = broader(office_id)
            target = broader_by_name.get(row["name"])
            broader_siblings = (target or {}).get("co_located") or siblings
            data_at = _sibling_data_at(broader_siblings, broader_by_name, parameter)
        row["data_at"] = data_at if row.get("parameter_count", 0) == 0 else []

    kept: list[dict[str, Any]] = []
    dropped = 0
    promoted_keys: set[tuple[str, str]] = set()
    promoted_rows: list[dict[str, Any]] = []

    for row in enriched:
        annotate_data_at(row)
        if parameter is None:
            kept.append(row)
            continue
        if parameter in (row.get("parameters") or []):
            kept.append(row)
            continue
        promoted_here = _promote_parameter_siblings(
            row, parameter, broader, promoted_keys, promoted_rows
        )
        if promoted_here and row.get("parameter_count", 0) == 0:
            # Barren parent whose siblings publish the parameter — keep
            # as an explicit discovery hint alongside the promoted siblings.
            row["data_at"] = sorted(promoted_here)
            kept.append(row)
        else:
            dropped += 1

    _append_unique_promotions(kept, enriched, promoted_rows)
    return kept, dropped


def _promote_parameter_siblings(
    row: dict[str, Any],
    parameter: str,
    broader: Any,
    promoted_keys: set[tuple[str, str]],
    promoted_rows: list[dict[str, Any]],
) -> list[str]:
    """Find broader-catalog siblings publishing `parameter`; append to the
    shared promoted-rows accumulator. Returns the names found, in input
    order, so the caller can populate `data_at` on a barren parent."""
    broader_by_name = broader(row["office_id"])
    target = broader_by_name.get(row["name"])
    co_loc = (target or {}).get("co_located") or row.get("co_located") or []
    found: list[str] = []
    for sibling_name in co_loc:
        key = (row["office_id"], sibling_name)
        if key in promoted_keys:
            continue
        sibling = broader_by_name.get(sibling_name)
        if not sibling:
            continue
        if parameter not in (sibling.get("parameters") or []):
            continue
        promoted_keys.add(key)
        sibling_copy = dict(sibling)
        sibling_copy["data_at"] = []
        promoted_rows.append(sibling_copy)
        found.append(sibling_name)
    return found


def _append_unique_promotions(
    kept: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    promoted_rows: list[dict[str, Any]],
) -> None:
    """Append promoted siblings to `kept`, skipping any name already
    surfaced as a direct query match or earlier in `kept`."""
    enriched_keys = {(r["office_id"], r["name"]) for r in enriched}
    kept_keys = {(r["office_id"], r["name"]) for r in kept}
    for sibling in promoted_rows:
        key = (sibling["office_id"], sibling["name"])
        if key in enriched_keys or key in kept_keys:
            continue
        kept.append(sibling)
        kept_keys.add(key)


def _sibling_data_at(
    siblings: list[str],
    by_name: dict[str, dict[str, Any]],
    parameter: str | None = None,
) -> list[str]:
    """Shared predicate: filter siblings to those that publish data.

    When `parameter` is set, further filter to siblings that publish that
    specific parameter. Stable ordering so snapshot tests don't churn.
    """
    out: list[str] = []
    for s in siblings:
        info = by_name.get(s)
        if not info or info.get("parameter_count", 0) <= 0:
            continue
        if parameter is not None and parameter not in (info.get("parameters") or []):
            continue
        out.append(s)
    return sorted(out)


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


def _data_at_for_location(
    office: str,
    name: str,
    *,
    use_cache: bool,
) -> list[str]:
    """Find data-bearing co-located siblings for a single named location.

    Used by `list_parameters` when the requested location is barren. Pulls
    the unfiltered enriched office catalog (cached) and applies the shared
    `_sibling_data_at` predicate. Returns an empty list when no sibling has
    data or when the location isn't in the office catalog at all.
    """
    enriched = catalog.enrich_locations(office, use_cache=use_cache)
    target = next((r for r in enriched if r["name"] == name), None)
    if target is None:
        return []
    siblings = target.get("co_located") or []
    if not siblings:
        return []
    by_name = {r["name"]: r for r in enriched}
    return _sibling_data_at(siblings, by_name)


def browse_region(
    *,
    office: str,
    bbox: BBox | None = None,
    state: str | None = None,
    limit: int | None = DEFAULT_BROWSE_LIMIT,
    cursor: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_browse_region` — enriched catalog filtered by office, bbox, or state.

    Filtering happens client-side because CDA doesn't expose bbox or radius
    queries. Co-location is computed across the office's full catalog before
    bbox filtering so siblings outside the bbox can still be flagged.

    `limit` caps the number of results (default 50). A no-filter browse of a
    large office can return thousands of rows; the cap keeps the response
    bounded. Set `limit=None` (or `limit=0` on the CLI) for no cap. When the
    cap kicks in the response carries `has_more: true`, `total_count`,
    `next_cursor`, and a `truncation_hint`. Data-bearing rows sort ahead of
    ghosts so a capped browse keeps the useful records.

    Pass the opaque `next_cursor` from a prior response as `cursor` to fetch
    the next page. The cursor encodes the request fingerprint; passing a cursor
    from a different request (different office/state/bbox) raises
    ``INVALID_CURSOR``.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer or None")
    if limit == 0:
        limit = None  # 0 means "no cap"

    req = pagination.request_hash({"office": office, "bbox": _bbox_to_dict(bbox), "state": state})
    decoded: dict[str, Any] | None = None
    offset = 0
    if cursor is not None:
        decoded = pagination.decode_cursor(cursor)
        offset = pagination.validate_continuation(decoded, kind="browse_region", req=req)

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

    # Data-bearing first, then ghosts; stable by office/name. Mirrors
    # `cwms_search_places` so a capped browse keeps the useful rows.
    rows = sorted(rows, key=lambda r: (-r["parameter_count"], r["office_id"], r["name"]))
    total_count = len(rows)
    ghost_count = sum(1 for r in rows if r["parameter_count"] == 0)
    if decoded is not None:
        pagination.ensure_total(decoded, total=total_count)  # catalog-shift guard
    if limit is None:
        rows = rows[offset:]
        has_more = False
    else:
        rows = rows[offset : offset + limit]
        has_more = offset + limit < total_count
    next_cursor: str | None = None
    if has_more and limit is not None:
        next_cursor = pagination.encode_cursor(
            {
                "v": pagination.CURSOR_VERSION,
                "kind": "browse_region",
                "off": offset + limit,
                "req": req,
                "total": total_count,
            }
        )

    # Index the FULL office catalog (pre-filter) so a barren row can name
    # co-located siblings that publish data even when those siblings fall
    # outside the bbox/state filter — matching the `data_at` repair hint from
    # search and the co-location-before-filtering note above.
    by_name = {r["name"]: r for r in enriched}

    def _data_at(row: dict[str, Any]) -> list[str]:
        if row.get("parameter_count", 0) != 0:
            return []
        return _sibling_data_at(row.get("co_located") or [], by_name)

    # Drop the verbose `raw` field for region browse responses — agents asking
    # for a region overview don't need every per-row DTO. They can fetch
    # cwms_describe_place for any specific hit.
    response: dict[str, Any] = {
        "office": office,
        "bbox": _bbox_to_dict(bbox),
        "state": state,
        "result_count": len(rows),
        "ghost_count": ghost_count,
        "total_count": total_count,
        "truncated": has_more,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": limit,
        "results": [
            {
                "office_id": r["office_id"],
                "name": r["name"],
                "public_name": r.get("public_name"),
                "location_kind": r.get("location_kind"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "parameter_count": r["parameter_count"],
                "parameters": r.get("parameters", []),
                "publishers": r["publishers"],
                "last_data_timestamp": r.get("last_data_timestamp"),
                "co_located": r.get("co_located", []),
                "data_at": _data_at(r),
            }
            for r in rows
        ],
    }
    if has_more:
        response["truncation_hint"] = (
            f"returned {len(rows)} of {total_count}; fetch the next page with the "
            "`next_cursor`, or pass --limit 0 for all rows"
        )
    return response


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
