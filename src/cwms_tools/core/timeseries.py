"""Windowed timeseries fetch with canonical-publisher selection and truncation detection.

Wraps `cwms.timeseries.timeseries.get_timeseries` with `multithread=False`
(we own concurrency at the tool layer — see `core/concurrency.py`) and
detects the silent truncation at the upstream `page_size=300000` cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import cwms.timeseries.timeseries as ts_api

from cwms_tools.core import catalog, publishers
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

# Upstream wrapper's default page_size for get_timeseries.
_UPSTREAM_PAGE_SIZE_CAP = 300_000


def canonical_ts_id(
    office: str,
    location: str,
    parameter: str,
    *,
    use_cache: bool = True,
) -> str | None:
    """Pick the canonical (best-publisher) ts_id for `(location, parameter)`.

    Returns None when no ts_id with the requested parameter exists at the
    location — in that case the caller raises NOT_FOUND with a repair hint.
    """
    ts_ids = catalog.ts_ids_for_location(office, location, use_cache=use_cache)
    return publishers.pick_canonical(ts_ids, parameter=parameter)


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
    data = ts_api.get_timeseries(
        ts_id=ts_id,
        office_id=office,
        begin=begin,
        end=end,
        unit=unit,
        multithread=False,
    )
    payload = data.json if hasattr(data, "json") else data
    truncated = _detect_truncation(payload)
    return {
        "ts_id": ts_id,
        "office_id": office,
        "unit": payload.get("units") or payload.get("unit") or unit,
        "begin": begin.isoformat(),
        "end": end.isoformat(),
        "values": _values_from_payload(payload),
        "truncated": truncated,
        "truncation_hint": (
            "hit upstream page cap of 300000; narrow the window or split the request"
            if truncated
            else None
        ),
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
    end = datetime.now(tz=timezone.utc)
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
    """`canonical_ts_id` that raises `NOT_FOUND` with a repair hint when nothing matches."""
    tsid = canonical_ts_id(office, location, parameter, use_cache=use_cache)
    if tsid is None:
        raise CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            f"No published ts_id at {office}/{location} for parameter {parameter!r}.",
            field="parameter",
            offending_value=parameter,
            hint=(
                "Use cwms_list_parameters to see what publishes at this location. "
                "Ghost records (parameter_count=0) carry no timeseries."
            ),
            repair=RepairHint(
                tool="cwms_list_parameters",
                args={"office": office, "name": location},
            ),
        )
    return tsid


def _detect_truncation(payload: dict[str, Any]) -> bool:
    values = payload.get("values")
    if not isinstance(values, list):
        return False
    return len(values) >= _UPSTREAM_PAGE_SIZE_CAP


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
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
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
    "canonical_ts_id",
    "fetch_latest",
    "fetch_window",
    "require_canonical_ts_id",
]
