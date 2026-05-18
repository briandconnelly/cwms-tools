"""Paginated catalog browse + enrichment.

The plan's highest-leverage value-add (§10.2): when an agent asks the catalog
for matches, the response already carries ghost detection, the publisher
fingerprint at each location, last-data freshness, and co-located siblings.
That turns "20 hits, call list_parameters on each" into "20 hits, agent
immediately knows which carry data".

Implemented as pure wrappers over `cwms.catalog.get_locations_catalog` and
`cwms.catalog.get_timeseries_catalog`, with the on-disk cache facade in
front of both. Live CDA hits go through `core.session` for the User-Agent
+ pool-sizing contract.
"""

from __future__ import annotations

import re
from typing import Any

import cwms.catalog.catalog as catalog_api

from cwms_tools.core import publishers
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint
from cwms_tools.core.geo import GeoPoint, co_located
from cwms_tools.core.session import current_config

# NW Division district stubs — short-circuit with a repair hint (§6.1).
_NW_STUBS: frozenset[str] = frozenset({"NWO", "NWK", "NWS", "NWP", "NWW"})
_NW_REPAIR_TARGETS: dict[str, str] = {
    "NWO": "NWDM",
    "NWK": "NWDM",
    "NWS": "NWDP",
    "NWP": "NWDP",
    "NWW": "NWDP",
}


def _raise_ghost_office(office_id: str) -> None:
    target = _NW_REPAIR_TARGETS.get(office_id, "NWDM")
    raise CwmsToolsError.of(
        ErrorCode.GHOST_OFFICE,
        f"Office {office_id} publishes no operational data; use the regional rollup.",
        field="office_id",
        offending_value=office_id,
        hint=(
            "NW Division districts (NWO, NWK, NWS, NWP, NWW) are catalog stubs. "
            "Use NWDM (Missouri) or NWDP (Pacific NW) instead."
        ),
        repair=RepairHint(tool="cwms_browse_region", args={"office": target}),
    )


def get_locations_catalog(
    office_id: str,
    *,
    like: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the paginated locations catalog for an office. Cached for 6 h."""
    if office_id in _NW_STUBS:
        _raise_ghost_office(office_id)
    cache = get_cache()
    ttl = cache.ttl_for("location_catalog")
    cfg = current_config()
    key = build_cache_key("location_catalog", office_id, like or "", api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    data = catalog_api.get_locations_catalog(office_id=office_id, like=like)
    payload = data.json
    cache.set(key, payload, ttl=ttl)
    return payload


def get_timeseries_catalog(
    office_id: str,
    *,
    like: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the paginated ts catalog for an office. Cached for 6 h."""
    if office_id in _NW_STUBS:
        _raise_ghost_office(office_id)
    cache = get_cache()
    ttl = cache.ttl_for("ts_catalog")
    cfg = current_config()
    key = build_cache_key("ts_catalog", office_id, like or "", api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    data = catalog_api.get_timeseries_catalog(office_id=office_id, like=like)
    payload = data.json
    cache.set(key, payload, ttl=ttl)
    return payload


def ts_ids_for_location(
    office_id: str,
    location: str,
    *,
    use_cache: bool = True,
) -> list[str]:
    """Return all distinct ts_ids whose location segment matches `location`."""
    payload = get_timeseries_catalog(office_id, use_cache=use_cache)
    out: list[str] = []
    seen: set[str] = set()
    for row in _iter_ts_entries(payload):
        tsid = row.get("name") or row.get("timeseries-id") or row.get("time-series-id")
        if not isinstance(tsid, str):
            continue
        if not tsid.startswith(f"{location}."):
            continue
        if tsid not in seen:
            seen.add(tsid)
            out.append(tsid)
    return out


def freshness_for_location(
    office_id: str,
    location: str,
    *,
    use_cache: bool = True,
) -> str | None:
    """Return the most-recent observed timestamp across the location's ts_ids.

    Reads `last-update` / `last_update_timestamp` from the ts catalog payload
    when present; falls back to None when CDA does not surface a freshness
    timestamp. Cheap to compute since the catalog payload is already cached.
    """
    payload = get_timeseries_catalog(office_id, use_cache=use_cache)
    best: str | None = None
    for row in _iter_ts_entries(payload):
        tsid = row.get("name") or row.get("timeseries-id") or row.get("time-series-id")
        if not isinstance(tsid, str) or not tsid.startswith(f"{location}."):
            continue
        for key in ("last-update", "last_update", "latest-time", "end"):
            ts = row.get(key)
            if isinstance(ts, str) and (best is None or ts > best):
                best = ts
    return best


def enrich_locations(
    office_id: str,
    *,
    like: str | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return the locations catalog plus per-record enrichment.

    Each record carries: `parameter_count`, `publishers`, `last_data_timestamp`,
    `co_located` (other ids within ~100 m of the same coordinates).
    """
    loc_payload = get_locations_catalog(office_id, like=like, use_cache=use_cache)
    rows = list(_iter_location_entries(loc_payload))
    if like:
        rows = [r for r in rows if _matches_like(r, like)]

    geopoints: list[GeoPoint] = [g for g in (_to_geopoint(r) for r in rows) if g is not None]
    geopoint_index = {(g.office_id, g.name): g for g in geopoints}

    # Scope the ts-catalog query to the matched names when a name filter is in
    # play. The full ts catalog for a big office is tens of thousands of rows;
    # a name-scoped query is typically dozens. The cache key includes the
    # `like` value so scoped and unscoped fetches never collide.
    ts_like: str | None = None
    if like:
        matched_names = sorted({r.get("name") for r in rows if isinstance(r.get("name"), str)})
        if not matched_names:
            return []
        # CDA's `like` parameter is a regex against the ts_id. Anchor at start
        # and alternate over the matched names so the response only contains
        # ts_ids whose location segment is one of them.
        ts_like = f"^({'|'.join(re.escape(n) for n in matched_names)})\\."
    ts_payload = get_timeseries_catalog(office_id, like=ts_like, use_cache=use_cache)
    by_location: dict[str, list[str]] = {}
    by_location_last: dict[str, str | None] = {}
    for ts_row in _iter_ts_entries(ts_payload):
        tsid = ts_row.get("name") or ts_row.get("timeseries-id") or ts_row.get("time-series-id")
        if not isinstance(tsid, str):
            continue
        parts = publishers.parse_ts_id(tsid)
        if parts is None:
            continue
        by_location.setdefault(parts.location, []).append(tsid)
        for key in ("last-update", "last_update", "latest-time", "end"):
            ts = ts_row.get(key)
            if isinstance(ts, str):
                cur = by_location_last.get(parts.location)
                if cur is None or ts > cur:
                    by_location_last[parts.location] = ts

    enriched: list[dict[str, Any]] = []
    for r in rows:
        name = r.get("name") or r.get("location-id")
        if not isinstance(name, str):
            continue
        loc_ts = by_location.get(name, [])
        params = publishers.parameter_counts(loc_ts)
        pubs = [f.publisher for f in publishers.aggregate_publishers(loc_ts)]
        target_gp = geopoint_index.get((office_id, name))
        siblings = (
            [g.name for g in co_located(target_gp, geopoints)] if target_gp is not None else []
        )
        enriched.append(
            {
                "office_id": office_id,
                "name": name,
                "public_name": r.get("public-name") or r.get("public_name"),
                "location_kind": r.get("location-kind") or r.get("kind"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "parameter_count": len(params),
                "publishers": pubs,
                "last_data_timestamp": by_location_last.get(name),
                "co_located": siblings,
                "raw": r,
            }
        )
    return enriched


def _iter_location_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Tolerate CDA's two common shapes for the locations catalog payload."""
    if isinstance(payload, list):
        return payload  # already-flat list
    for key in ("entries", "items", "locations", "values"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    inner = payload.get("locations")
    if isinstance(inner, dict):
        for key in ("location", "entries", "items"):
            v = inner.get(key)
            if isinstance(v, list):
                return v
    return []


def _iter_ts_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Tolerate CDA's two common shapes for the timeseries catalog payload."""
    if isinstance(payload, list):
        return payload
    for key in ("entries", "items", "timeseries", "time-series", "values"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []


def _matches_like(row: dict[str, Any], needle: str) -> bool:
    n = needle.casefold()
    for key in ("name", "location-id", "public-name", "long-name", "description"):
        v = row.get(key)
        if isinstance(v, str) and n in v.casefold():
            return True
    return False


def _to_geopoint(row: dict[str, Any]) -> GeoPoint | None:
    office = row.get("office-id") or row.get("office")
    name = row.get("name") or row.get("location-id")
    lat = row.get("latitude")
    lon = row.get("longitude")
    if (
        not isinstance(office, str)
        or not isinstance(name, str)
        or not isinstance(lat, (int, float))
        or not isinstance(lon, (int, float))
    ):
        return None
    return GeoPoint(office_id=office, name=name, latitude=float(lat), longitude=float(lon))


__all__ = [
    "enrich_locations",
    "freshness_for_location",
    "get_locations_catalog",
    "get_timeseries_catalog",
    "ts_ids_for_location",
]
