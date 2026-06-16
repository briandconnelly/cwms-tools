"""Task-completing logic for `cwms_get_value` and `cwms_get_history`.

These tools combine timeseries reads with the applicable-threshold lookup so
the most common §9 task ("what's the current value of X at place Y?")
resolves in one tool call rather than four.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from cwms_tools.core import catalog, depth, levels, publishers, timeseries
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.models import Rollup

#: Allowed `rollup` modes for `get_history`, derived from the `Rollup` enum so
#: the enum stays the single source of truth (no drift). `raw` returns every
#: point; `hourly`/`daily` return per-bucket min/max/mean/count instead.
ROLLUP_MODES: tuple[str, ...] = tuple(r.value for r in Rollup)

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
            status_budget_seconds if status_budget_seconds is not None else _STATUS_BUDGET_SECONDS
        )
        thresholds_active, status_class, level_lookup_status = _resolve_thresholds_with_timeout(
            timeout=budget,
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
        "level_lookup_status": level_lookup_status,
        "truncated": series.get("truncated", False),
        "truncation_hint": series.get("truncation_hint"),
    }


def get_profile(
    office: str,
    name: str,
    parameter: str,
    *,
    window: timedelta = timedelta(hours=24),
    unit: str = "EN",
    use_cache: bool = True,
) -> dict[str, Any]:
    """Read every depth-tagged sensor of one string in a single call (#26/#27).

    `name` is the parent "string" location (e.g. `GWLW_S1`). This finds the
    co-located depth-tagged sensors that publish `parameter` (e.g.
    `GWLW_S1-D3,0ft`, `-D13,0ft`, ...), fetches each one's latest value, and
    returns them as a profile sorted shallow→deep with structured
    `depth: {value, unit}` per sensor — so a stratification question is one
    round-trip instead of one call per depth, with no need to decode the
    cryptic `D<n>,0ft` tag.
    """
    if window <= timedelta(0):
        raise CwmsToolsError.of(
            ErrorCode.USAGE_ERROR,
            "window_hours must be a positive number of hours.",
            field="window_hours",
            offending_value=int(window.total_seconds() // 3600),
            hint="Pass a positive look-back window, e.g. window_hours=24.",
        )
    # `enrich_locations(like=...)` still fetches the full per-office locations
    # catalog (cached) and filters it client-side, but the `like` scopes the
    # potentially-large timeseries-catalog enrichment to this string's depth
    # children (`<parent>-D...`) instead of the whole office. That client-side
    # filter is a plain substring match (no regex); the server-side ts `like` is
    # built from re.escape'd, anchored row names; and the `startswith(prefix)`
    # filter below is the precise selector — so the parent name can't widen or
    # inject into the upstream query.
    prefix = f"{name}-D"
    enriched = catalog.enrich_locations(office, like=prefix, use_cache=use_cache)
    children: list[tuple[str, dict[str, Any]]] = []
    for row in enriched:
        loc = row["name"]
        if not loc.startswith(prefix):
            continue
        parsed = depth.parse_depth(loc)
        if parsed is None or parameter not in row.get("parameters", []):
            continue
        children.append((loc, parsed))
    children.sort(key=lambda c: depth.depth_sort_key(c[0]))

    profile = [
        _profile_entry(office, loc, parameter, parsed, window=window, unit=unit)
        for loc, parsed in children
    ]
    # Top-level `unit` mirrors the other value tools: the actual measurement
    # unit of the readings (e.g. degF, ft), taken from the first sensor read
    # successfully; fall back to the requested unit system if every read failed.
    measured_unit = next((e["unit"] for e in profile if e.get("unit")), unit)
    response: dict[str, Any] = {
        "office_id": office,
        "name": name,
        "parameter": parameter,
        "unit": measured_unit,
        "sensor_count": len(profile),
        "profile": profile,
    }
    if not profile:
        response["note"] = (
            f"No depth-tagged sensors under {name!r} publish {parameter!r} in {office}. "
            "Confirm the parent string id and parameter with cwms_list_parameters / "
            "cwms_search_places, or read a single sensor with cwms_get_value."
        )
    return response


def _profile_entry(
    office: str,
    location: str,
    parameter: str,
    parsed_depth: dict[str, Any],
    *,
    window: timedelta,
    unit: str,
) -> dict[str, Any]:
    """One profile row: depth metadata plus the sensor's latest observation.

    A per-sensor failure degrades to `value: null` with an `error` code rather
    than failing the whole profile, so one dead sensor doesn't sink the read.
    """
    entry: dict[str, Any] = {"name": location, "depth": parsed_depth}
    try:
        point = get_value(
            office, location, parameter, window=window, unit=unit, classify_against_levels=False
        )
    except CwmsToolsError as err:
        entry.update(value=None, timestamp=None, error=err.envelope.code.value)
        return entry
    entry.update(
        value=point.get("value"),
        unit=point.get("unit", unit),
        timestamp=point.get("timestamp"),
        publisher=point.get("publisher"),
        ts_id=point.get("ts_id"),
    )
    return entry


def get_history(
    office: str,
    location: str,
    parameter: str,
    *,
    begin: datetime,
    end: datetime,
    unit: str = "EN",
    rollup: str = "raw",
) -> dict[str, Any]:
    """Windowed history for one (office, location, parameter).

    `rollup` controls the value shape (token cost): `raw` (default) returns
    every point; `hourly`/`daily` server-side downsample to per-bucket
    min/max/mean/count, so a trend question over a long window costs a handful
    of rows instead of hundreds. A `summary` block (first/last/min/max/mean/
    delta/count over the window) is always included so the most common
    "how has X changed?" question needs no client-side reduction.
    """
    if rollup not in ROLLUP_MODES:
        raise CwmsToolsError.of(
            ErrorCode.USAGE_ERROR,
            f"Unknown rollup {rollup!r}.",
            field="rollup",
            offending_value=rollup,
            hint=f"Use one of: {', '.join(ROLLUP_MODES)}.",
        )
    tsid = timeseries.require_canonical_ts_id(office, location, parameter)
    parts = publishers.parse_ts_id(tsid)
    series = timeseries.fetch_window(tsid, office=office, begin=begin, end=end, unit=unit)
    values = series["values"]
    response: dict[str, Any] = {
        "ts_id": tsid,
        "office_id": office,
        "location": location,
        "parameter": parameter,
        "publisher": parts.version if parts else None,
        "unit": series.get("unit", unit),
        "begin": series["begin"],
        "end": series["end"],
        "rollup": rollup,
        "value_count": len(values),
        "summary": _summarize(values),
        "truncated": series.get("truncated", False),
        "truncation_hint": series.get("truncation_hint"),
        "next_begin": series.get("next_begin"),
    }
    if rollup == "raw":
        response["values"] = values
    else:
        # Rolled-up: omit the raw points (the whole point is fewer rows) and
        # return the per-bucket aggregates instead.
        response["values"] = []
        response["buckets"] = _bucketize(values, rollup)
    return response


def _summarize(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    """First/last/min/max/mean/delta/count over the non-null observations.

    `first`/`last` are by timestamp (earliest/latest), derived independently of
    the input ordering — RFC3339 UTC timestamps sort lexicographically, so an
    out-of-order or unsorted window still yields correct first/last/delta. Only
    observations that have both a numeric value AND a string timestamp are
    considered (consistent with `timeseries._latest_point`), so a value with no
    timestamp can't skew first/last. Timestamps are not re-parsed here — the
    upstream emits RFC3339 UTC strings that sort correctly as-is. Returns None
    when the window holds no such observations.
    """
    numeric = [
        v
        for v in values
        if isinstance(v.get("value"), (int, float)) and isinstance(v.get("timestamp"), str)
    ]
    if not numeric:
        return None
    ordered = sorted(numeric, key=lambda v: v["timestamp"])
    nums = [float(v["value"]) for v in ordered]
    return {
        "count": len(nums),
        "first": nums[0],
        "last": nums[-1],
        "min": min(nums),
        "max": max(nums),
        "mean": sum(nums) / len(nums),
        "delta": nums[-1] - nums[0],
    }


def _bucketize(values: list[dict[str, Any]], rollup: str) -> list[dict[str, Any]]:
    """Group points into UTC hour/day buckets with min/max/mean/count.

    Buckets are half-open intervals floored to the UTC hour (`hourly`) or UTC
    calendar day (`daily`); the bucket `timestamp` is the interval start. Only
    numeric observations contribute; empty buckets are not emitted. Bucketing
    on UTC keeps the boundaries deterministic and independent of the series'
    local timezone.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for point in values:
        value = point.get("value")
        ts = point.get("timestamp")
        if not isinstance(value, (int, float)) or not isinstance(ts, str):
            continue
        key = _bucket_key(ts, rollup)
        if key is None:
            continue
        agg = buckets.get(key)
        fvalue = float(value)
        if agg is None:
            buckets[key] = {
                "timestamp": key,
                "min": fvalue,
                "max": fvalue,
                "_sum": fvalue,
                "count": 1,
            }
        else:
            agg["min"] = min(agg["min"], fvalue)
            agg["max"] = max(agg["max"], fvalue)
            agg["_sum"] += fvalue
            agg["count"] += 1
    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        agg = buckets[key]
        out.append(
            {
                "timestamp": agg["timestamp"],
                "min": agg["min"],
                "max": agg["max"],
                "mean": agg["_sum"] / agg["count"],
                "count": agg["count"],
            }
        )
    return out


def _bucket_key(timestamp: str, rollup: str) -> str | None:
    """Floor an RFC3339 UTC timestamp to its hour/day bucket start (RFC3339).

    Returns None for an unparseable timestamp or an unexpected rollup (callers
    validate `rollup` ∈ {hourly, daily}; this stays defensive rather than
    silently treating an unknown mode as daily)."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    if rollup == "hourly":
        floored = dt.replace(minute=0, second=0, microsecond=0)
    elif rollup == "daily":
        floored = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        return None
    return floored.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    now = datetime.now(tz=UTC)
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


__all__ = ["get_history", "get_profile", "get_value"]
