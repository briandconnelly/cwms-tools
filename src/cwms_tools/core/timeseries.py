"""Windowed timeseries fetch with canonical-publisher selection and truncation detection.

Wraps `cwms.timeseries.timeseries.get_timeseries` with `multithread=False`
(we own concurrency at the tool layer — see `core/concurrency.py`) and
detects the silent truncation at the upstream `page_size=300000` cap.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime, timedelta
from typing import Any

import cwms.timeseries.timeseries as ts_api

from cwms_tools.core import catalog, depth, publishers
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent
from cwms_tools.core.upstream import require_collection, upstream_call

# Upstream wrapper's default page_size for get_timeseries.
_UPSTREAM_PAGE_SIZE_CAP = 300_000


def fetch_window(
    ts_id: str,
    *,
    office: str,
    begin: datetime,
    end: datetime,
    unit: str = "EN",
) -> dict[str, Any]:
    """Fetch a windowed timeseries via cwms-python.

    Forces `multithread=False` (the bounded executor at the tool layer owns
    concurrency) and detects silent truncation at the wrapper's 300 000-point
    cap so callers can warn agents instead of returning partial data.
    """
    with upstream_call("/timeseries", operation="timeseries fetch", page_selector="values"):
        data = ts_api.get_timeseries(
            ts_id=ts_id,
            office_id=office,
            begin=begin,
            end=end,
            unit=unit,
            multithread=False,
        )
    # A real empty window still carries `values: []`; `{}` is an empty body (#152).
    payload = require_collection(
        data.json if hasattr(data, "json") else data,
        keys=("values",),
        endpoint="/timeseries",
        operation="timeseries fetch",
        field="ts_id",
        value=ts_id,
    )
    truncated = _detect_truncation(payload, requested_end=end)
    next_begin = _next_begin(payload) if truncated else None
    if not truncated:
        truncation_hint = None
    elif next_begin is not None:
        truncation_hint = (
            f"hit upstream page cap of {_UPSTREAM_PAGE_SIZE_CAP}; retry with "
            "begin_iso=<next_begin> to continue, or narrow the window."
        )
    else:
        truncation_hint = (
            f"hit upstream page cap of {_UPSTREAM_PAGE_SIZE_CAP} but could not derive a "
            "continuation timestamp; narrow the window and re-request."
        )
    return {
        "ts_id": ts_id,
        "office_id": office,
        "unit": payload.get("units") or payload.get("unit") or unit,
        "begin": begin.isoformat(),
        "end": end.isoformat(),
        "values": _values_from_payload(payload),
        "truncated": truncated,
        "next_begin": next_begin,
        "truncation_hint": truncation_hint,
        "raw": payload,
    }


def fetch_latest(
    ts_id: str,
    *,
    office: str,
    window: timedelta = timedelta(hours=24),
    unit: str = "EN",
) -> dict[str, Any]:
    """Fetch the most recent observation in the last `window` seconds.

    Returns the highest-timestamp row with `value` populated, or None if the
    window is empty.
    """
    end = datetime.now(tz=UTC)
    begin = end - window
    series = fetch_window(ts_id, office=office, begin=begin, end=end, unit=unit)
    latest = _latest_point(series["values"])
    return {
        **series,
        "latest": latest,
    }


def require_canonical_ts_id(
    office: str,
    location: str,
    parameter: str,
    *,
    use_cache: bool = True,
) -> str:
    """The canonical ts_id for `(office, location, parameter)`, or a self-repairing error.

    One catalog fetch, three failure branches (#96/#163):
    A. the location publishes other parameters -> `not_found` naming them, with
       a same-tool intent on the closest published code;
    B/C. the location has no timeseries -> first confirm the catalog row exists
       (else `not_found{field:name}` with a spelling suggestion), then
       `ghost_location` pointing at data-bearing co-located siblings (B) or,
       when none exist, at `cwms_search_places` (C).

    The failure path reads each catalog once: the locations catalog fetched for
    the existence check feeds the suggestion and the single office-wide
    enrichment, which both sibling filters (with and without `parameter`) share.
    """
    ts_ids = catalog.ts_ids_for_location(office, location, use_cache=use_cache)
    tsid = publishers.pick_canonical(ts_ids, parameter=parameter)
    if tsid is not None:
        return tsid
    if ts_ids:
        raise _parameter_not_found(office, location, parameter, ts_ids)
    loc_payload = catalog.get_locations_catalog(office, use_cache=use_cache)
    if catalog.location_catalog_row(office, location, payload=loc_payload) is None:
        raise catalog.location_not_found_error(
            office, location, payload=loc_payload, endpoints_called=["/catalog/TIMESERIES"]
        )
    by_name = catalog.enriched_by_name(office, use_cache=use_cache, loc_payload=loc_payload)
    raise _ghost_location_error(office, location, parameter, by_name)


def _closest_parameter(parameter: str, published: list[str]) -> str | None:
    """Case-insensitive exact match first (`elev` -> `Elev`), then difflib."""
    for code in published:
        if code.casefold() == parameter.casefold():
            return code
    close = difflib.get_close_matches(parameter, published, n=1, cutoff=0.6)
    return close[0] if close else None


def _parameter_not_found(
    office: str, location: str, parameter: str, ts_ids: list[str]
) -> CwmsToolsError:
    published = sorted(publishers.parameter_counts(ts_ids))
    match = _closest_parameter(parameter, published)
    reason = f"This location publishes: {', '.join(published)}. Retry with one of these."
    if match is not None:
        reason += f" Did you mean {match!r}?"
    return CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        f"No published ts_id at {office}/{location} for parameter {parameter!r}.",
        field="parameter",
        value=parameter,
        reason=reason,
        repair=RepairHint(
            next_step="list_available_parameters",
            tool="cwms_list_parameters",
            arguments={"office": office, "name": location},
        ),
        repair_intent=(
            RepairIntent("use_published_parameter", {"parameter": match}) if match else None
        ),
        endpoints_called=["/catalog/TIMESERIES"],
    )


_GHOST_TRAIL = ["/catalog/TIMESERIES", "/catalog/LOCATIONS"]


def _ghost_location_error(
    office: str, location: str, parameter: str, by_name: dict[str, dict[str, Any]]
) -> CwmsToolsError:
    with_param = catalog.siblings_with_data(by_name, location, parameter)
    any_data = with_param or catalog.siblings_with_data(by_name, location)
    if not any_data:  # Case C: the genuine dead end
        return CwmsToolsError.of(
            ErrorCode.GHOST_LOCATION,
            f"{office}/{location} publishes no timeseries (ghost record), and no "
            "co-located record does either.",
            field="name",
            value=location,
            reason=(
                "The catalog row exists but carries no data, and nothing within "
                "~100 m publishes. Search for the place by name to find the "
                "data-bearing record."
            ),
            repair=RepairHint(
                next_step="search_for_data_bearing_place",
                tool="cwms_search_places",
                arguments={"query": location, "office": office},
            ),
            endpoints_called=list(_GHOST_TRAIL),
        )
    # Case B: siblings carry the data. Prefer the shallowest depth child of THIS
    # string (so the intent is a child of the requested name, not a neighbour);
    # else a single flat sibling; several flat candidates -> no same-tool intent,
    # the search repair lists them all with data_at.
    prefix = f"{location}-D"
    depth_children = sorted(
        (s for s in with_param if s.startswith(prefix) and depth.parse_depth(s) is not None),
        key=depth.depth_sort_key,
    )
    intent: RepairIntent | None = None
    if depth_children:
        intent = RepairIntent(
            "read_data_bearing_sibling",
            {"name": depth_children[0]},
            alternative=(
                f"cwms_get_profile with office={office!r}, name={location!r}, "
                f"parameter={parameter!r} reads every depth of this string in one call."
            ),
        )
    elif len(with_param) == 1:
        intent = RepairIntent("read_data_bearing_sibling", {"name": with_param[0]})
    listed = with_param or any_data
    scope = (
        f"publish {parameter!r}" if with_param else f"publish data (none publishes {parameter!r})"
    )
    return CwmsToolsError.of(
        ErrorCode.GHOST_LOCATION,
        f"{office}/{location} publishes no timeseries (ghost record); co-located "
        "records carry the data.",
        field="name",
        value=location,
        reason=f"Co-located records that {scope}: {', '.join(listed)}.",
        repair=RepairHint(
            next_step="search_for_data_bearing_place",
            tool="cwms_search_places",
            arguments={"query": location, "office": office, "parameter": parameter},
        ),
        repair_intent=intent,
        endpoints_called=list(_GHOST_TRAIL),
    )


def _next_begin(payload: dict[str, Any]) -> str | None:
    """RFC3339 timestamp one millisecond after the latest returned point.

    Used as the `begin` of the next slice when a window truncated at the page
    cap, so the continuation has no duplicate or skipped seam point (CWMS
    point timestamps are millisecond-resolution).
    """
    values = payload.get("values")
    if not isinstance(values, list):
        return None
    last_ms: int | None = None
    for row in reversed(values):
        if isinstance(row, list) and row and isinstance(row[0], (int, float)):
            last_ms = int(row[0])
            break
    if last_ms is None:
        return None
    return _ms_to_rfc3339(last_ms + 1)


def _detect_truncation(payload: dict[str, Any], *, requested_end: datetime) -> bool:
    """Flag truncation only when the upstream returned the page-cap count AND
    the last point timestamp is earlier than the requested end of window.

    The bare row-count check is necessary but not sufficient — many small
    windows naturally return < 300 000 points; many large windows that we
    fully fulfill happen to return exactly the cap. We also require a gap
    between the last returned timestamp and the requested end.
    """
    values = payload.get("values")
    if not isinstance(values, list) or len(values) < _UPSTREAM_PAGE_SIZE_CAP:
        return False
    last_ms: int | None = None
    for row in reversed(values):
        if isinstance(row, list) and row and isinstance(row[0], (int, float)):
            last_ms = int(row[0])
            break
    if last_ms is None:
        return True  # cap-sized response with no parseable timestamp; safer to flag
    last_dt = datetime.fromtimestamp(last_ms / 1000, tz=UTC)
    return last_dt < requested_end


def _values_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the upstream `values` array-of-arrays into a list of dicts.

    The upstream format is `[ [date_ms, value, quality], ... ]` keyed by
    the `value-columns` field. Our normalized shape carries `timestamp`
    (RFC3339 UTC), `value` (float|None), and `quality` (int|None).
    """
    raw_values = payload.get("values")
    if not isinstance(raw_values, list):
        return []
    cols = payload.get("value-columns") or payload.get("valueColumns") or []
    col_names = [c.get("name") for c in cols if isinstance(c, dict)]
    out: list[dict[str, Any]] = []
    for row in raw_values:
        if not isinstance(row, list) or len(row) < 2:
            continue
        ts_ms = row[0]
        value = row[1]
        quality = row[2] if len(row) > 2 else None
        _ = col_names  # reserved for future column-aware decoding
        out.append(
            {
                "timestamp": _ms_to_rfc3339(ts_ms),
                "value": _coerce_float(value),
                "quality": quality,
            }
        )
    return out


def _latest_point(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [v for v in values if v.get("value") is not None and v.get("timestamp")]
    if not valid:
        return None
    return max(valid, key=lambda v: v["timestamp"])


def _ms_to_rfc3339(ts_ms: Any) -> str | None:
    if ts_ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
    except (ValueError, TypeError, OSError):
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "fetch_latest",
    "fetch_window",
    "require_canonical_ts_id",
]
