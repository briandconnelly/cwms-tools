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
from urllib.parse import quote

import cwms.catalog.catalog as catalog_api
from cwms.api import ApiError

from cwms_tools.core import publishers
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    RepairHint,
    upstream_error_from_status,
)
from cwms_tools.core.geo import GeoPoint, co_located
from cwms_tools.core.session import current_config


def _wrap_api_error(exc: ApiError, *, endpoint: str) -> CwmsToolsError:
    """Turn a cwms-python ApiError into a status-classified CwmsToolsError.

    The user-facing `message` deliberately omits `ApiError.__str__()`, which
    embeds the full request URL — for broad searches the URL contains a
    multi-kilobyte alternation regex and floods the response. The endpoint
    path is already in `endpoints_called`; the original exception is still
    reachable via `__cause__` for debugging.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return upstream_error_from_status(
        status,
        endpoint=endpoint,
        message=f"Upstream returned {status or 'error'} for {endpoint}.",
    )


# URL-encoded byte budget for the ts catalog `like` parameter. CDA rejects
# very large alternation regexes with a 500; the empirical threshold is
# uncertain but ~3 KB fails. This cap stays well under that boundary.
# When the candidate `like` regex would exceed this, `enrich_locations`
# skips ts-catalog enrichment for the affected rows and marks them
# truncated rather than hitting the upstream with a too-large request.
MAX_TS_LIKE_BYTES: int = 2048

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
    try:
        data = catalog_api.get_locations_catalog(office_id=office_id, like=like)
    except ApiError as exc:
        raise _wrap_api_error(exc, endpoint="/catalog/LOCATIONS") from exc
    payload = data.json
    cache.set(key, payload, ttl=ttl)
    return payload


def get_timeseries_catalog(
    office_id: str,
    *,
    like: str | None = None,
    include_extents: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the paginated ts catalog for an office. Cached for 6 h.

    `include_extents` controls whether each row carries the `extents`
    array with `latest-time` / `last-update` / `earliest-time`. The
    enriched response shape needs them for freshness; `ts_ids_for_location`
    and `canonical_ts_id` don't, so the flag stays off by default.
    Requesting extents materially enlarges the response (tens of times
    larger for big offices), so flipping this for queries that don't
    need it is what made `value get` unusable in evaluation.
    """
    if office_id in _NW_STUBS:
        _raise_ghost_office(office_id)
    cache = get_cache()
    ttl = cache.ttl_for("ts_catalog")
    cfg = current_config()
    key = build_cache_key(
        "ts_catalog",
        office_id,
        like or "",
        "extents" if include_extents else "no-extents",
        api_root=cfg.api_root,
    )
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    try:
        data = catalog_api.get_timeseries_catalog(
            office_id=office_id, like=like, include_extents=include_extents
        )
    except ApiError as exc:
        raise _wrap_api_error(exc, endpoint="/catalog/TIMESERIES") from exc
    payload = data.json
    cache.set(key, payload, ttl=ttl)
    return payload


def _row_latest_time(row: dict[str, Any]) -> str | None:
    """Pluck the most-recent observation timestamp from a ts catalog row.

    Prefers `latest-time` from `row["extents"][...]` (the canonical field
    when `include_extents=True`). The CDA distinction matters: `latest-time`
    is when the observation occurred; `last-update` is when CWMS wrote it
    to its store, which is slightly later. Agents asking for "freshness"
    want the observation time. Falls back to other field names for
    forward/backward compatibility with older catalog shapes.
    """
    extents = row.get("extents")
    if isinstance(extents, list):
        best: str | None = None
        for ext in extents:
            if not isinstance(ext, dict):
                continue
            ts = ext.get("latest-time")
            if isinstance(ts, str) and (best is None or ts > best):
                best = ts
        if best is not None:
            return best
        # If no `latest-time` was present anywhere, fall back to `last-update`.
        for ext in extents:
            if not isinstance(ext, dict):
                continue
            ts = ext.get("last-update")
            if isinstance(ts, str) and (best is None or ts > best):
                best = ts
        if best is not None:
            return best
    for key in ("latest-time", "last-update", "last_update", "end"):
        ts = row.get(key)
        if isinstance(ts, str):
            return ts
    return None


def ts_ids_for_location(
    office_id: str,
    location: str,
    *,
    use_cache: bool = True,
) -> list[str]:
    """Return all distinct ts_ids whose location segment matches `location`.

    Scopes the ts catalog request to `^<location>\\.` so we don't pull the
    full office catalog (tens of thousands of rows for NWDM) every time
    `value get` or `value history` resolves a canonical ts_id.
    """
    like = f"^{re.escape(location)}\\."
    payload = get_timeseries_catalog(office_id, like=like, use_cache=use_cache)
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
    like = f"^{re.escape(location)}\\."
    payload = get_timeseries_catalog(
        office_id, like=like, include_extents=True, use_cache=use_cache
    )
    best: str | None = None
    for row in _iter_ts_entries(payload):
        tsid = row.get("name") or row.get("timeseries-id") or row.get("time-series-id")
        if not isinstance(tsid, str) or not tsid.startswith(f"{location}."):
            continue
        ts = _row_latest_time(row)
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def _dedupe_rows_by_name(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CDA returns multiple rows per `name` (alias / bounding-office variants).

    Dedupe so the enriched response has at most one entry per location.
    """
    rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for r in raw_rows:
        n = r.get("name") or r.get("location-id")
        if not isinstance(n, str) or n in seen_names:
            continue
        seen_names.add(n)
        rows.append(r)
    return rows


def _build_ts_like_for_rows(rows: list[dict[str, Any]]) -> tuple[str | None, bool]:
    """Return `(ts_like_or_none, truncated)`.

    Builds the `^(name1|...|nameN)\\.` alternation. When the URL-encoded
    form would push the request past `MAX_TS_LIKE_BYTES`, returns
    `(None, True)` so callers can skip the ts-catalog fetch — that path
    is what an unbounded fallback would 500 on at the upstream.
    """
    candidate_like = f"^({'|'.join(re.escape(r['name']) for r in rows)})\\."
    if len(quote(candidate_like, safe="")) <= MAX_TS_LIKE_BYTES:
        return candidate_like, False
    return None, True


def _index_ts_payload(
    ts_payload: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, str | None]]:
    """Group ts ids by location and track the latest observation per location."""
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
        ts = _row_latest_time(ts_row)
        if ts is not None:
            cur = by_location_last.get(parts.location)
            if cur is None or ts > cur:
                by_location_last[parts.location] = ts
    return by_location, by_location_last


def enrich_locations(
    office_id: str,
    *,
    like: str | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return the locations catalog plus per-record enrichment.

    Each record carries: `parameter_count`, `publishers`, `last_data_timestamp`,
    `co_located` (other ids within ~100 m of the same coordinates).

    Note on the `like` filter: CDA's server-side `like` on
    `/catalog/LOCATIONS` matches the location id only — a search for
    "Fort Peck" would return zero rows because the canonical id is
    "FTPK". So we always fetch the full per-office locations catalog
    (cached for 6 hours) and filter client-side across `name`,
    `public-name`, `long-name`, and `description`.
    """
    loc_payload = get_locations_catalog(office_id, use_cache=use_cache)
    raw_rows = list(_iter_location_entries(loc_payload))
    if like:
        raw_rows = [r for r in raw_rows if _matches_like(r, like)]
    rows = _dedupe_rows_by_name(raw_rows)
    if like and not rows:
        return []

    geopoints: list[GeoPoint] = [g for g in (_to_geopoint(r) for r in rows) if g is not None]
    geopoint_index = {(g.office_id, g.name): g for g in geopoints}

    # Scope the ts-catalog query to the matched names when a name filter
    # is in play. Without a filter the ts catalog is too large to fetch
    # with extents; agents who want freshness should describe a specific
    # place.
    enrichment_truncated = False
    if like:
        ts_like, enrichment_truncated = _build_ts_like_for_rows(rows)
    else:
        ts_like = None

    if enrichment_truncated:
        ts_payload: dict[str, Any] = {"entries": []}
    else:
        ts_payload = get_timeseries_catalog(
            office_id,
            like=ts_like,
            include_extents=ts_like is not None,
            use_cache=use_cache,
        )
    by_location, by_location_last = _index_ts_payload(ts_payload)

    enriched: list[dict[str, Any]] = []
    for r in rows:
        name = r["name"]
        loc_ts = by_location.get(name, [])
        params = publishers.parameter_counts(loc_ts)
        pubs = [f.publisher for f in publishers.aggregate_publishers(loc_ts)]
        target_gp = geopoint_index.get((office_id, name))
        siblings = (
            [g.name for g in co_located(target_gp, geopoints)] if target_gp is not None else []
        )
        entry: dict[str, Any] = {
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
        if enrichment_truncated:
            entry["enrichment_truncated"] = True
            entry["enrichment_truncated_reason"] = "alternation_overflow"
        enriched.append(entry)
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
