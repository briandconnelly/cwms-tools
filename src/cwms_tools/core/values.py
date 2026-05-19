"""Task-completing logic for `cwms_get_value` and `cwms_get_history`.

These tools combine timeseries reads with the applicable-threshold lookup so
the most common §9 task ("what's the current value of X at place Y?")
resolves in one tool call rather than four.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from cwms_tools.core import levels, publishers, timeseries

_threading = threading  # keep import live in case the formatter strips it

# How long the threshold/status lookup is allowed to take before the caller
# is given back an unclassified value. CWMS's `/levels` endpoint is
# unreliably slow for big offices (NWDM reliably exceeds 60s). The chosen
# budget tries to balance "computed-most-of-the-time-on-warm-cache" against
# "an interactive caller never waits indefinitely". Tunable via env.
_STATUS_BUDGET_SECONDS: float = 8.0


def get_value(
    office: str,
    location: str,
    parameter: str,
    *,
    window: timedelta = timedelta(hours=24),
    unit: str = "EN",
    classify_against_levels: bool = False,
    status_budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Latest value for one (office, location, parameter).

    Threshold classification against CWMS Location Levels is OFF by default.
    The `/levels` endpoint is reliably slow (8 s budget exceeded on every
    cold-cache call in evaluation), so the value-only path is the fast
    default. Set `classify_against_levels=True` to opt into the slower
    classified path.

    Response always carries `level_lookup_status` so the caller can
    distinguish "skipped on purpose" from "attempted but unavailable":

    - `"skipped"`: classification was not requested.
    - `"computed"`: thresholds were resolved and applied.
    - `"timed_out"`: the upstream level lookup exceeded the budget; the
      in-flight HTTP continues so the next invocation should warm-hit.
    - `"unavailable"`: the lookup returned but no thresholds matched
      (could be "no levels defined" or a transient upstream failure).

    `status_budget_seconds` (default 8 s) caps how long the threshold
    lookup may run when classification is requested.
    """
    tsid = timeseries.require_canonical_ts_id(office, location, parameter)
    parts = publishers.parse_ts_id(tsid)
    series = timeseries.fetch_latest(tsid, office=office, window=window, unit=unit)
    latest = series.get("latest") or {}
    observation = latest.get("value") if isinstance(latest, dict) else None
    timestamp = latest.get("timestamp") if isinstance(latest, dict) else None

    thresholds_active: list[dict[str, Any]] = []
    status_class = "unknown"
    level_lookup_status = "skipped"
    if classify_against_levels and observation is not None:
        budget = (
            status_budget_seconds
            if status_budget_seconds is not None
            else _STATUS_BUDGET_SECONDS
        )
        thresholds_active, status_class, level_lookup_status = (
            _resolve_thresholds_with_timeout(
                timeout=budget,
                office=office,
                location=location,
                parameter=parameter,
                observation=observation,
                unit=series.get("unit", unit),
            )
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
        "level_lookup_status": level_lookup_status,
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


def _resolve_thresholds_with_timeout(
    *,
    timeout: float,
    office: str,
    location: str,
    parameter: str,
    observation: float,
    unit: str,
) -> tuple[list[dict[str, Any]], str, str]:
    """Run the threshold lookup with a wall-clock budget.

    Uses a daemon thread (not the bounded executor) so the in-flight
    HTTP request doesn't block process exit when the caller hits
    timeout. The work continues until the upstream returns or the
    process dies; if it returns in time it writes the response to
    cache, which makes the next invocation fast.

    Returns `(thresholds_active, status_class, level_lookup_status)`.
    `level_lookup_status` is one of: `computed`, `timed_out`,
    `unavailable` (lookup completed but found no thresholds).
    """
    box: dict[str, Any] = {"result": None, "error": None}
    done = threading.Event()

    def _target() -> None:
        try:
            box["result"] = _resolve_thresholds(
                office=office,
                location=location,
                parameter=parameter,
                observation=observation,
                unit=unit,
            )
        except Exception as exc:
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(
        target=_target,
        name=f"cwms-tools-thresholds-{office}/{location}/{parameter}",
        daemon=True,
    )
    thread.start()
    if not done.wait(timeout):
        return [], "unknown", "timed_out"
    if box["error"] is not None or box["result"] is None:
        return [], "unknown", "unavailable"
    thresholds, status = box["result"]
    if not thresholds and status == "unknown":
        # `_resolve_thresholds` returns ([], "unknown") both when the upstream
        # lookup fails and when no levels are defined. We can't distinguish
        # without inspecting further, so report `unavailable` and let the
        # caller decide whether to retry.
        return thresholds, status, "unavailable"
    return thresholds, status, "computed"


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
