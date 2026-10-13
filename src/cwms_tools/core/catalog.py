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

import difflib
import math
import re
from typing import Any

import cwms.catalog.catalog as catalog_api

from cwms_tools.core import publishers
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent
from cwms_tools.core.geo import GeoPoint, co_located_index
from cwms_tools.core.offices import NW_STUBS, ghost_office_error
from cwms_tools.core.session import current_config
from cwms_tools.core.upstream import empty_collection_error, upstream_call

# Byte limit (UTF-8, before URL encoding) on the ts catalog `like` pattern.
# CDA answers a longer pattern with `500 Database Error`: measured live on
# 2026-10-13 in SAM and SWT, 512 bytes passes and 513 fails, which matches
# Oracle's REGEXP_LIKE pattern limit (#190). `enrich_locations` packs its
# name alternation into batches that each fit.
MAX_TS_LIKE_BYTES: int = 512

# Most `like` batches one filtered `enrich_locations` call sends (#190). It
# bounds logical ts-catalog calls per office, not HTTP pages or a whole
# search's work; rows past it come back with `enrichment_incomplete:
# batch_limit`. Eight batches hold roughly 150-400 names
# (SAM's 58 `Carters*` ids, about 22 bytes each, need three).
MAX_TS_LIKE_BATCHES: int = 8


def get_locations_catalog(
    office_id: str,
    *,
    like: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the paginated locations catalog for an office. Cached for 6 h."""
    if office_id in NW_STUBS:
        raise ghost_office_error(office_id)
    cache = get_cache()
    ttl = cache.ttl_for("location_catalog")
    key = _location_catalog_key(office_id, like)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    with upstream_call(
        "/catalog/LOCATIONS", operation="locations catalog", page_selector="entries"
    ):
        data = catalog_api.get_locations_catalog(office_id=office_id, like=like)
    payload = data.json
    if _location_entries(payload) is None:
        raise empty_collection_error(
            endpoint="/catalog/LOCATIONS",
            operation="locations catalog",
            field="office_id",
            value=office_id,
        )
    cache.set(key, payload, ttl=ttl)
    return payload


def _location_catalog_key(office_id: str, like: str | None) -> str:
    cfg = current_config()
    return build_cache_key("location_catalog", office_id, like or "", api_root=cfg.api_root)


def location_catalog_cached(office_id: str, *, like: str | None = None) -> bool:
    """True iff `get_locations_catalog(office_id, like=)` is a cache hit.

    Like `ts_catalog_cached`, the single place that knows the key shape, so
    probes (`places`, `offices`) cannot drift from the producer.
    """
    return get_cache().get(_location_catalog_key(office_id, like)) is not None


def _ts_catalog_key(office_id: str, *, like: str | None, include_extents: bool) -> str:
    cfg = current_config()
    return build_cache_key(
        "ts_catalog",
        office_id,
        like or "",
        "extents" if include_extents else "no-extents",
        api_root=cfg.api_root,
    )


def ts_catalog_cached(
    office_id: str, *, like: str | None = None, include_extents: bool = False
) -> bool:
    """True iff `get_timeseries_catalog(office_id, like=, include_extents=)` is a cache hit.

    The single place that knows the ts_catalog key shape — probes elsewhere
    (`publishers_index`) drifted from it once (#164).
    """
    key = _ts_catalog_key(office_id, like=like, include_extents=include_extents)
    return get_cache().get(key) is not None


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
    (and so `timeseries.require_canonical_ts_id`) doesn't, so the flag stays
    off by default.
    Requesting extents materially enlarges the response (tens of times
    larger for big offices), so flipping this for queries that don't
    need it is what made `value get` unusable in evaluation.
    """
    if office_id in NW_STUBS:
        raise ghost_office_error(office_id)
    cache = get_cache()
    ttl = cache.ttl_for("ts_catalog")
    key = _ts_catalog_key(office_id, like=like, include_extents=include_extents)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    with upstream_call(
        "/catalog/TIMESERIES", operation="timeseries catalog", page_selector="entries"
    ):
        data = catalog_api.get_timeseries_catalog(
            office_id=office_id, like=like, include_extents=include_extents
        )
    payload = data.json
    if _ts_entries(payload) is None:
        raise empty_collection_error(
            endpoint="/catalog/TIMESERIES",
            operation="timeseries catalog",
            field="office_id",
            value=office_id,
        )
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


def _like_for_names(names: list[str]) -> str:
    """The anchored `^(name1|...|nameN)\\.` alternation over escaped names."""
    return f"^({'|'.join(re.escape(n) for n in names)})\\."


def _like_bytes(like: str) -> int:
    return len(like.encode("utf-8"))


def _pack_ts_like_batches(names: list[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Greedily pack `names` into `^(...)\\.` patterns of at most `MAX_TS_LIKE_BYTES`.

    Returns `(batches, oversize)`: each batch is `(like, names_in_it)`, in
    input order; `oversize` lists names whose own pattern is already over
    the limit, so no request can carry them.
    """
    batches: list[tuple[str, list[str]]] = []
    oversize: list[str] = []
    current: list[str] = []
    for name in names:
        if _like_bytes(_like_for_names([name])) > MAX_TS_LIKE_BYTES:
            oversize.append(name)
            continue
        if current and _like_bytes(_like_for_names([*current, name])) > MAX_TS_LIKE_BYTES:
            batches.append((_like_for_names(current), current))
            current = []
        current.append(name)
    if current:
        batches.append((_like_for_names(current), current))
    return batches, oversize


def _index_ts_payload(
    ts_payload: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, str | None], dict[str, set[str]]]:
    """Group ts ids by location and track the latest observation per location.

    Also tracks the distinct parameters published at each location; the
    third return value is `{location_name: {parameter, ...}}` and lets the
    enriched response carry a per-row `parameters` list for downstream
    parameter-aware filtering (`cwms_search_places(parameter=...)`).
    """
    by_location: dict[str, list[str]] = {}
    by_location_last: dict[str, str | None] = {}
    by_location_params: dict[str, set[str]] = {}
    for ts_row in _iter_ts_entries(ts_payload):
        tsid = ts_row.get("name") or ts_row.get("timeseries-id") or ts_row.get("time-series-id")
        if not isinstance(tsid, str):
            continue
        parts = publishers.parse_ts_id(tsid)
        if parts is None:
            continue
        by_location.setdefault(parts.location, []).append(tsid)
        by_location_params.setdefault(parts.location, set()).add(parts.parameter)
        ts = _row_latest_time(ts_row)
        if ts is not None:
            cur = by_location_last.get(parts.location)
            if cur is None or ts > cur:
                by_location_last[parts.location] = ts
    return by_location, by_location_last, by_location_params


def _fetch_ts_batches(
    office_id: str,
    names: list[str],
    incomplete: dict[str, tuple[str, CwmsToolsError | None]],
    *,
    use_cache: bool,
) -> dict[str, Any]:
    """Fetch the extents ts catalog for `names` in `like`-sized batches (#190).

    Returns the merged entries. Names left unenriched go into `incomplete` as
    `name -> (reason, error)`: `pattern_too_long` (no request can carry it),
    `batch_limit` (past `MAX_TS_LIKE_BATCHES`), or `upstream_error` (its
    batch's request failed). A failed batch degrades only its own rows, and
    failures are never cached.
    """
    batches, oversize = _pack_ts_like_batches(names)
    for name in oversize:
        incomplete[name] = ("pattern_too_long", None)
    for _, skipped in batches[MAX_TS_LIKE_BATCHES:]:
        for name in skipped:
            incomplete[name] = ("batch_limit", None)
    entries: list[dict[str, Any]] = []
    for like, batch_names in batches[:MAX_TS_LIKE_BATCHES]:
        try:
            payload = get_timeseries_catalog(
                office_id, like=like, include_extents=True, use_cache=use_cache
            )
        except CwmsToolsError as err:
            for name in batch_names:
                incomplete[name] = ("upstream_error", err)
            continue
        entries.extend(_iter_ts_entries(payload))
    return {"entries": entries}


def enrich_locations(
    office_id: str,
    *,
    like: str | None = None,
    use_cache: bool = True,
    loc_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the locations catalog plus per-record enrichment.

    Each record carries: `parameter_count`, `publishers`, `last_data_timestamp`,
    `co_located` (other ids within ~100 m of the same coordinates).

    With `like`, rows whose enrichment could not be fetched carry
    `enrichment_incomplete` (`pattern_too_long`, `batch_limit`, or
    `upstream_error`, the last with an internal `enrichment_error`
    `{code, temporary}`); their zero counts mean "unknown", not "ghost".
    Without `like`, an upstream failure still raises.

    Note on the `like` filter: CDA's server-side `like` on
    `/catalog/LOCATIONS` matches the location id only — a search for
    "Fort Peck" would return zero rows because the canonical id is
    "FTPK". So we always fetch the full per-office locations catalog
    (cached for 6 hours) and filter client-side across `name`,
    `public-name`, `long-name`, and `description`.

    `loc_payload` lets a caller that already holds this office's locations
    catalog reuse it, so a `use_cache=False` path fetches it once (#96).
    """
    if loc_payload is None:
        loc_payload = get_locations_catalog(office_id, use_cache=use_cache)
    raw_rows = list(_iter_location_entries(loc_payload))
    if like:
        raw_rows = [r for r in raw_rows if _matches_like(r, like)]
    rows = _dedupe_rows_by_name(raw_rows)
    if like and not rows:
        return []

    geopoints: list[GeoPoint] = [g for g in (_to_geopoint(r) for r in rows) if g is not None]
    # One-pass sibling index (#169): the pairwise form was O(N²) and took
    # 160 s on NWDM's 10 k rows even with the catalog on disk.
    siblings_by_key = co_located_index(geopoints)

    # Scope the ts-catalog query to the matched names when a name filter
    # is in play. Without a filter the ts catalog is too large to fetch
    # with extents; agents who want freshness should describe a specific
    # place.
    incomplete: dict[str, tuple[str, CwmsToolsError | None]] = {}
    if like:
        ts_payload = _fetch_ts_batches(
            office_id, [r["name"] for r in rows], incomplete, use_cache=use_cache
        )
    else:
        ts_payload = get_timeseries_catalog(office_id, use_cache=use_cache)
    by_location, by_location_last, by_location_params = _index_ts_payload(ts_payload)

    enriched: list[dict[str, Any]] = []
    for r in rows:
        name = r["name"]
        loc_ts = by_location.get(name, [])
        params = publishers.parameter_counts(loc_ts)
        pubs = [f.publisher for f in publishers.aggregate_publishers(loc_ts)]
        siblings = [g.name for g in siblings_by_key.get((office_id, name), [])]
        entry: dict[str, Any] = {
            "office_id": office_id,
            "name": name,
            "public_name": r.get("public-name") or r.get("public_name"),
            "location_kind": r.get("location-kind") or r.get("kind"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "parameter_count": len(params),
            "parameters": sorted(by_location_params.get(name, set())),
            "publishers": pubs,
            "last_data_timestamp": by_location_last.get(name),
            "co_located": siblings,
            "raw": r,
        }
        if name in incomplete:
            reason, err = incomplete[name]
            entry["enrichment_incomplete"] = reason
            if err is not None:
                # Internal: lets callers report the code / temporary flag;
                # never projected into a response.
                entry["enrichment_error"] = {
                    "code": err.envelope.code.value,
                    "temporary": err.envelope.temporary,
                }
        enriched.append(entry)
    return enriched


def _location_entries(payload: Any) -> list[dict[str, Any]] | None:
    """Rows of a locations catalog payload, or None when no known shape matches.

    Tolerates CDA's common shapes. None (e.g. the `{}` cwms-python returns for
    an empty body) is distinct from a real empty catalog, `{"entries": []}`.
    """
    if isinstance(payload, list):
        return payload  # already-flat list
    if not isinstance(payload, dict):
        return None
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
    return None


def _iter_location_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _location_entries(payload) or []


def _ts_entries(payload: Any) -> list[dict[str, Any]] | None:
    """Rows of a timeseries catalog payload, or None when no known shape matches."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("entries", "items", "timeseries", "time-series", "values"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return None


def _iter_ts_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _ts_entries(payload) or []


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
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    return GeoPoint(office_id=office, name=name, latitude=float(lat), longitude=float(lon))


def sibling_data_at(
    siblings: list[str],
    by_name: dict[str, dict[str, Any]],
    parameter: str | None = None,
) -> list[str]:
    """Filter `siblings` to those that publish data (and `parameter`, when set); sorted.

    The one sibling predicate shared by search/browse/describe/list_parameters
    and the value tools' ghost repair (#96) — moved here from `places` so it
    cannot fork (#45/#55/#56).
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


def enriched_by_name(
    office_id: str,
    *,
    use_cache: bool = True,
    loc_payload: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """The full-office enrichment indexed by location name.

    Build it once per call and apply `siblings_with_data` as often as needed:
    each `enrich_locations` pass fetches the office-wide timeseries catalog
    (refetched on a `use_cache=False` path) and rebuilds the co-location
    index (#96).
    """
    enriched = enrich_locations(office_id, use_cache=use_cache, loc_payload=loc_payload)
    return {r["name"]: r for r in enriched}


def siblings_with_data(
    by_name: dict[str, dict[str, Any]], name: str, parameter: str | None = None
) -> list[str]:
    """Co-located siblings of `name` in `by_name` that publish data (optionally `parameter`)."""
    target = by_name.get(name)
    if target is None:
        return []
    return sibling_data_at(target.get("co_located") or [], by_name, parameter)


def data_bearing_siblings(
    office_id: str,
    name: str,
    *,
    parameter: str | None = None,
    use_cache: bool = True,
    loc_payload: dict[str, Any] | None = None,
) -> list[str]:
    """Co-located siblings of `name` that publish data (optionally `parameter`).

    `[]` when `name` is not in the office catalog or no sibling publishes —
    callers that need to tell those apart check `location_catalog_row` first.
    """
    by_name = enriched_by_name(office_id, use_cache=use_cache, loc_payload=loc_payload)
    return siblings_with_data(by_name, name, parameter)


def location_catalog_row(
    office_id: str,
    name: str,
    *,
    use_cache: bool = True,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The exact-name (case-sensitive) locations-catalog row, or None if absent.

    `payload` reuses a locations catalog the caller already fetched for this office.
    """
    payload = _payload_or_fetch(office_id, payload, use_cache=use_cache)
    for row in _iter_location_entries(payload):
        if (row.get("name") or row.get("location-id")) == name:
            return row
    return None


def _payload_or_fetch(
    office_id: str, payload: dict[str, Any] | None, *, use_cache: bool
) -> dict[str, Any]:
    """`payload` when the caller already holds the locations catalog, else fetch it."""
    if payload is None:
        return get_locations_catalog(office_id, use_cache=use_cache)
    return payload


def suggest_location(
    office_id: str,
    name: str,
    *,
    use_cache: bool = True,
    payload: dict[str, Any] | None = None,
) -> tuple[str | None, list[str]]:
    """`(exact_ci_match, fuzzy)`: the catalog id whose id/public/long name equals
    `name` case-insensitively, else up to three fuzzy candidates (#163).

    Fuzzy = difflib close matches over every label (cutoff 0.6) plus labels
    whose first word equals the query's first word ("Foss Lake" → FOSS via
    "Foss Reservoir"). Pure string matching: no regex is built from `name`.
    `payload` reuses a locations catalog the caller already fetched.
    """
    payload = _payload_or_fetch(office_id, payload, use_cache=use_cache)
    rows = _dedupe_rows_by_name(list(_iter_location_entries(payload)))
    folded = name.casefold().strip()
    ids: dict[str, str] = {}  # casefolded catalog id -> catalog id
    aliases: dict[str, list[str]] = {}  # casefolded public/long name -> catalog ids
    for row in rows:
        catalog_id = row.get("name") or row.get("location-id")
        if not isinstance(catalog_id, str):
            continue
        ids.setdefault(catalog_id.casefold(), catalog_id)
        for label in (row.get("public-name"), row.get("long-name")):
            if isinstance(label, str) and label:
                bucket = aliases.setdefault(label.casefold(), [])
                if catalog_id not in bucket:
                    bucket.append(catalog_id)
    # A catalog id beats an alias; an alias is a repairable match only when it
    # names exactly one record — two "Shared" public names must not become an
    # arbitrary same-tool retry (Codex review). Ambiguous aliases become fuzzy
    # candidates for discovery instead.
    if folded in ids:
        return ids[folded], []
    alias_hits = aliases.get(folded, [])
    if len(alias_hits) == 1:
        return alias_hits[0], []
    fuzzy: list[str] = list(alias_hits)
    # Ids are applied last so an alias equal to another row's id cannot
    # shadow that id's label.
    labels: dict[str, str] = {**{k: v[0] for k, v in aliases.items()}, **ids}
    for key in difflib.get_close_matches(folded, list(labels), n=5, cutoff=0.6):
        if labels[key] not in fuzzy:
            fuzzy.append(labels[key])
    first = folded.split(" ", 1)[0]
    if len(first) >= 3:
        for key, catalog_id in labels.items():
            if key.split(" ", 1)[0] == first and catalog_id not in fuzzy:
                fuzzy.append(catalog_id)
    return None, fuzzy[:3]


def location_not_found_error(
    office_id: str,
    name: str,
    *,
    use_cache: bool = True,
    payload: dict[str, Any] | None = None,
    endpoints_called: list[str] | None = None,
) -> CwmsToolsError:
    """`not_found{field:"name"}` for a name absent from the office catalog (#163).

    Distinct from a ghost: the record does not exist at all. Carries a
    case-insensitive suggestion as a same-tool `RepairIntent` when unique, and
    always a generic `cwms_search_places` repair.

    `payload` reuses the locations catalog the caller already checked.
    `endpoints_called` names the endpoints the caller hit first;
    `/catalog/LOCATIONS` is appended so the provenance trail is complete.
    """
    exact, fuzzy = suggest_location(office_id, name, use_cache=use_cache, payload=payload)
    trail = [*(endpoints_called or []), "/catalog/LOCATIONS"]
    search = RepairHint(
        next_step="resolve_name_with_search",
        tool="cwms_search_places",
        arguments={"query": name, "office": office_id},
    )
    base = (
        "Location ids are case-sensitive catalog ids (e.g. FOSS, not 'Foss' or "
        "'Foss Lake'); resolve them with cwms_search_places."
    )
    if exact is not None:
        return CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            f"No location {name!r} in office {office_id}; did you mean {exact!r}?",
            field="name",
            value=name,
            reason=f"{base} {exact!r} matches case-insensitively; retry with name={exact!r}.",
            repair=search,
            repair_intent=RepairIntent("retry_with_catalog_id", {"name": exact}),
            endpoints_called=trail,
        )
    hint = f" Similar catalog ids: {', '.join(fuzzy)}." if fuzzy else ""
    return CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        f"No location {name!r} in office {office_id}.",
        field="name",
        value=name,
        reason=f"{base}{hint}",
        repair=search,
        endpoints_called=trail,
    )


__all__ = [
    "data_bearing_siblings",
    "enrich_locations",
    "enriched_by_name",
    "freshness_for_location",
    "get_locations_catalog",
    "get_timeseries_catalog",
    "location_catalog_cached",
    "location_catalog_row",
    "location_not_found_error",
    "sibling_data_at",
    "siblings_with_data",
    "suggest_location",
    "ts_catalog_cached",
    "ts_ids_for_location",
]
