"""Task-completing logic for `cwms_get_value` and `cwms_get_history`.

These tools combine timeseries reads with the applicable-threshold lookup so
the most common §9 task ("what's the current value of X at place Y?")
resolves in one tool call rather than four.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from cwms_tools.core import levels, publishers, timeseries


def get_value(
    office: str,
    location: str,
    parameter: str,
    *,
    window: timedelta = timedelta(hours=24),
    unit: str = "EN",
    classify_against_levels: bool = True,
) -> dict[str, Any]:
    """Latest value + inline status classification for one (office, location, parameter)."""
    tsid = timeseries.require_canonical_ts_id(office, location, parameter)
    parts = publishers.parse_ts_id(tsid)
    series = timeseries.fetch_latest(tsid, office=office, window=window, unit=unit)
    latest = series.get("latest") or {}
    observation = latest.get("value") if isinstance(latest, dict) else None
    timestamp = latest.get("timestamp") if isinstance(latest, dict) else None

    thresholds_active: list[dict[str, Any]] = []
    status_class = "unknown"
    if classify_against_levels and observation is not None:
        thresholds_active, status_class = _resolve_thresholds(
            office=office,
            location=location,
            parameter=parameter,
            observation=observation,
            unit=series.get("unit", unit),
        )

    return {
        "ts_id": tsid,
        "office_id": office,
        "location": location,
        "parameter": parameter,
        "publisher": parts.version if parts else None,
        "value": observation,
        "unit": series.get("unit", unit),
        "timestamp": timestamp,
        "status_class": status_class,
        "thresholds_active": thresholds_active,
        "truncated": series.get("truncated", False),
        "truncation_hint": series.get("truncation_hint"),
    }


def get_history(
    office: str,
    location: str,
    parameter: str,
    *,
    begin: datetime,
    end: datetime,
    unit: str = "EN",
) -> dict[str, Any]:
    """Windowed history for one (office, location, parameter)."""
    tsid = timeseries.require_canonical_ts_id(office, location, parameter)
    parts = publishers.parse_ts_id(tsid)
    series = timeseries.fetch_window(tsid, office=office, begin=begin, end=end, unit=unit)
    return {
        "ts_id": tsid,
        "office_id": office,
        "location": location,
        "parameter": parameter,
        "publisher": parts.version if parts else None,
        "unit": series.get("unit", unit),
        "begin": series["begin"],
        "end": series["end"],
        "values": series["values"],
        "value_count": len(series["values"]),
        "truncated": series.get("truncated", False),
        "truncation_hint": series.get("truncation_hint"),
    }


def _resolve_thresholds(
    *,
    office: str,
    location: str,
    parameter: str,
    observation: float,
    unit: str,
) -> tuple[list[dict[str, Any]], str]:
    """Find every level matching `<location>.<parameter>.*` and classify."""
    now = datetime.now(tz=timezone.utc)
    try:
        rows = levels.list_levels(office, location=location, parameter=parameter)
    except Exception:
        return [], "unknown"

    candidates: list[dict[str, Any]] = []
    for row in rows:
        level_id = row.get("location-level-id") or row.get("level-id") or row.get("locationLevelId")
        specified = row.get("specified-level-id") or row.get("specifiedLevelId")
        if not isinstance(level_id, str) or not isinstance(specified, str):
            continue
        eff = row.get("level-date") or row.get("levelDate")
        eff_dt = _coerce_dt(eff) or now
        try:
            level_value = levels.fetch_level_value(
                level_id, office=office, effective_date=eff_dt, unit=unit
            )
        except Exception:
            continue
        value = level_value.get("value")
        if not isinstance(value, (int, float)):
            continue
        candidates.append(
            {
                "specified_level_id": specified,
                "level_id": level_id,
                "value": float(value),
                "unit": level_value.get("unit", unit),
                "source_workaround": level_value.get("source_workaround"),
            }
        )

    status_class, annotated = levels.classify(observation, candidates)
    return annotated, status_class


def _coerce_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


__all__ = ["get_history", "get_value"]
