"""Location-level resolution + threshold classification.

Wraps `cwms.levels.location_levels` for listing and reading level
configurations. Includes the seasonal-level workaround
(`_workarounds.py::seasonal_level_as_ts`) for cwms-python issue #286:
when a level's variety is seasonal we bypass `get_level_as_timeseries`
and hit `/levels/{id}/timeseries` directly via `cwms.api.get`.

The workaround needs `(level_id, office, effective_date)` to identify the
right level revision, so we first list candidate levels and pick the row
with the latest `levelDate` ≤ the query window's start, expirationDate
either null or > window start.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import cwms.api as cwms_api
import cwms.levels.location_levels as ll_api

from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.session import current_config

# Substring fingerprint of the seasonal-bug detection. We treat any level
# whose configuration carries seasonalValues / intervalMonths / intervalMinutes
# as seasonal and route around `get_level_as_timeseries`.
_SEASONAL_KEYS = ("seasonal-values", "seasonalValues", "interval-months", "intervalMonths")


def list_levels(
    office: str,
    *,
    level_id_mask: str | None = None,
    location: str | None = None,
    parameter: str | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return location-level configurations matching the filters.

    `level_id_mask` accepts CDA's `*` glob syntax. When `location` and
    `parameter` are given without an explicit mask, we build the standard
    `<location>.<parameter>.*` mask from them.

    Cached for 24 h in the `levels` namespace. The CWMS `/levels`
    endpoint is reliably slow for big offices (NWDM can take a minute
    or more), so caching is what makes `value get` usable for repeat
    queries against the same place/parameter.
    """
    if level_id_mask is None and location and parameter:
        level_id_mask = f"{location}.{parameter}.*"

    cache = get_cache()
    ttl = cache.ttl_for("levels")
    cfg = current_config()
    cache_key = build_cache_key("levels", office, level_id_mask or "", api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit

    try:
        data = ll_api.get_location_levels(
            office_id=office,
            level_id_mask=level_id_mask,
        )
    except Exception as exc:
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            f"levels listing failed for {office} mask={level_id_mask!r}: {exc}",
            endpoints_called=["/levels"],
        ) from exc
    payload = data.json if hasattr(data, "json") else data
    rows = _iter_level_entries(payload)
    cache.set(cache_key, rows, ttl=ttl)
    return rows


def resolve_applicable_level(
    office: str,
    *,
    location: str,
    parameter: str,
    at: datetime,
) -> dict[str, Any] | None:
    """Pick the level row whose `levelDate` is the latest <= `at`.

    Honors `expirationDate`: a row is applicable only if expirationDate is
    null OR > `at`. Returns None when no row matches.
    """
    rows = list_levels(office, location=location, parameter=parameter)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        level_date = _coerce_dt(row.get("level-date") or row.get("levelDate"))
        if level_date is None or level_date > at:
            continue
        exp = _coerce_dt(row.get("expiration-date") or row.get("expirationDate"))
        if exp is not None and exp <= at:
            continue
        candidates.append((level_date, row))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[-1][1]


def fetch_level_value(
    level_id: str,
    *,
    office: str,
    effective_date: datetime,
    unit: str = "EN",
) -> dict[str, Any]:
    """Read a single level value at the given effective_date.

    For constant levels this returns the scalar directly. For seasonal levels
    we bypass `get_level_as_timeseries` (cwms-python issue #286 — see
    cwms-overview.md §8) and hit `/levels/{id}/timeseries` directly via
    `cwms.api.get`. The response carries `source_workaround: "issue-286"`
    when the bypass is taken.
    """
    try:
        data = ll_api.get_location_level(
            level_id=level_id,
            office_id=office,
            effective_date=effective_date,
            unit=unit,
        )
    except Exception as exc:
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            f"get_location_level failed for {office}/{level_id}: {exc}",
            endpoints_called=[f"/levels/{level_id}"],
        ) from exc
    payload = data.json if hasattr(data, "json") else data

    constant = _constant_value_from(payload)
    if constant is not None:
        return {
            "level_id": level_id,
            "office_id": office,
            "effective_date": effective_date.isoformat(),
            "variety": "constant",
            "value": constant,
            "unit": payload.get("level-units-id") or payload.get("levelUnitsId") or unit,
            "source_workaround": None,
        }

    if _is_seasonal(payload):
        return _seasonal_workaround(
            level_id=level_id,
            office=office,
            effective_date=effective_date,
            unit=unit,
            level_payload=payload,
        )

    # TimeSeries / Virtual / unknown varieties — return the bare payload as a
    # passthrough; future milestones can add proper handling.
    return {
        "level_id": level_id,
        "office_id": office,
        "effective_date": effective_date.isoformat(),
        "variety": "other",
        "value": None,
        "unit": payload.get("level-units-id") or unit,
        "source_workaround": None,
        "raw": payload,
    }


def classify(
    observation: float | None,
    thresholds: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Classify an observation against the active thresholds.

    Returns `(status_class, sorted_thresholds)`. `status_class` is one of
    `nominal | watch | action | flood | unknown`, derived from the highest
    threshold the observation has crossed.
    """
    if observation is None:
        return "unknown", thresholds
    annotated: list[dict[str, Any]] = []
    for t in thresholds:
        value = t.get("value")
        if not isinstance(value, (int, float)):
            continue
        if observation > value:
            relation = "above"
        elif observation < value:
            relation = "below"
        else:
            relation = "at"
        annotated.append(
            {
                **t,
                "relation": relation,
                "delta": observation - value,
            }
        )
    above = [t for t in annotated if t["relation"] in {"above", "at"}]
    annotated.sort(key=lambda t: -t["value"])
    if not above:
        return "nominal", annotated
    highest = max((t["specified_level_id"] for t in above), key=str.casefold, default="")
    name = highest.lower()
    if "flood" in name:
        return "flood", annotated
    if "action" in name or "warning" in name:
        return "action", annotated
    if "watch" in name or "monitor" in name:
        return "watch", annotated
    return "watch", annotated


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _iter_level_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    for key in ("entries", "items", "values", "levels"):
        v = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(v, list):
            return v
    if isinstance(payload, dict):
        # `get_location_levels` paginated wrapper sometimes returns the raw page.
        return [payload]
    return []


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


def _constant_value_from(payload: dict[str, Any]) -> float | None:
    for key in ("constant-value", "constantValue", "level-value", "levelValue"):
        v = payload.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _is_seasonal(payload: dict[str, Any]) -> bool:
    return any(payload.get(k) for k in _SEASONAL_KEYS)


def _seasonal_workaround(
    *,
    level_id: str,
    office: str,
    effective_date: datetime,
    unit: str,
    level_payload: dict[str, Any],
) -> dict[str, Any]:
    """Direct-CDA hit on `/levels/{id}/timeseries` to work around issue #286.

    The wrapper's `get_level_as_timeseries` returns wrong values for seasonal
    levels (cwms-python issue #286 — see cwms-overview.md §8). The fix is to
    bypass it and hit the underlying endpoint with both the resolved
    `effective-date` (which level revision to use) and a sensible 1-hour
    query window starting at `effective_date`.
    """

    window_end = effective_date + timedelta(hours=1)
    quoted = quote(level_id, safe="")
    params = {
        "office": office,
        "effective-date": effective_date.isoformat(),
        "begin": effective_date.isoformat(),
        "end": window_end.isoformat(),
        "interval": "1Hour",
        "unit": unit,
    }
    try:
        # No leading slash: BaseUrlSession concatenates `api_root` (which already
        # includes `cwms-data/`) with this relative path. A leading slash would
        # bypass the api_root prefix.
        raw = cwms_api.get(f"levels/{quoted}/timeseries", params)
    except Exception as exc:
        raise CwmsToolsError.of(
            ErrorCode.WRAPPER_BUG,
            f"seasonal-level workaround failed for {office}/{level_id}: {exc}",
            hint=(
                "Seasonal levels are routed around cwms-python issue #286 by "
                "hitting /levels/{id}/timeseries directly; this request failed."
            ),
            endpoints_called=[f"/levels/{level_id}/timeseries"],
            workaround="issue-286",
        ) from exc
    payload = raw if isinstance(raw, dict) else {}
    point = _first_value_from(payload)
    return {
        "level_id": level_id,
        "office_id": office,
        "effective_date": effective_date.isoformat(),
        "variety": "seasonal",
        "value": point,
        "unit": payload.get("units") or payload.get("unit") or unit,
        "source_workaround": "issue-286",
        "level_config": level_payload,
    }


def _first_value_from(payload: dict[str, Any]) -> float | None:
    values = payload.get("values")
    if isinstance(values, list) and values:
        row = values[0]
        if isinstance(row, list) and len(row) >= 2:
            v = row[1]
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
    return None


__all__ = [
    "classify",
    "fetch_level_value",
    "list_levels",
    "resolve_applicable_level",
]
