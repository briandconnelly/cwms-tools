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
from cwms_tools.core.errors import CwmsToolsError
from cwms_tools.core.session import current_config
from cwms_tools.core.upstream import (
    empty_collection_error,
    require_collection,
    require_record,
    upstream_call,
)

# Substring fingerprint of the seasonal-bug detection. We treat any level
# whose configuration carries seasonalValues / intervalMonths / intervalMinutes
# as seasonal and route around `get_level_as_timeseries`.
_SEASONAL_KEYS = ("seasonal-values", "seasonalValues", "interval-months", "intervalMonths")

# Spellings of a level row's id across CDA payload versions.
_LEVEL_ID_KEYS = ("location-level-id", "level-id", "locationLevelId")


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

    with upstream_call("/levels", operation=f"levels listing for {office} mask={level_id_mask!r}"):
        data = ll_api.get_location_levels(
            office_id=office,
            level_id_mask=level_id_mask,
        )
    payload = data.json if hasattr(data, "json") else data
    rows = _level_entries(payload)
    if rows is None:
        raise empty_collection_error(
            endpoint="/levels", operation="levels listing", field="office_id", value=office
        )
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
    survivors = applicable_revisions(rows, at=at)
    if not survivors:
        return None
    return max(
        survivors,
        key=lambda r: _coerce_dt(r.get("level-date") or r.get("levelDate")) or at,
    )


def applicable_revisions(rows: list[dict[str, Any]], *, at: datetime) -> list[dict[str, Any]]:
    """One row per `level_id`: the revision in force at `at`.

    CDA's `/levels` listing returns every revision of a level (#162: FOSS had
    a 1900 and a 1961 row for most ids). Keep, per id, the latest `level-date`
    <= `at` whose `expiration-date` is null or > `at`. Undated rows are
    skipped; equal dates keep listing order; output keeps first-seen id order.
    """
    best: dict[str, tuple[datetime, dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        level_id = level_id_of(row)
        if level_id is None:
            continue
        level_date = _coerce_dt(row.get("level-date") or row.get("levelDate"))
        if level_date is None or level_date > at:
            continue
        exp = _coerce_dt(row.get("expiration-date") or row.get("expirationDate"))
        if exp is not None and exp <= at:
            continue
        current = best.get(level_id)
        if current is None:
            order.append(level_id)
            best[level_id] = (level_date, row)
        elif level_date > current[0]:
            best[level_id] = (level_date, row)
    return [best[level_id][1] for level_id in order]


def fetch_level_value(
    level_id: str,
    *,
    office: str,
    effective_date: datetime,
    unit: str = "EN",
    at: datetime | None = None,
) -> dict[str, Any]:
    """Read a single level value.

    `effective_date` selects the level revision (the `effective-date` query
    param). `at` is the instant the threshold is evaluated (the observation
    timestamp); it defaults to `effective_date`. Only the seasonal bypass
    uses `at`, as its query window.

    For constant levels this returns the scalar directly. For seasonal levels
    we bypass `get_level_as_timeseries` (cwms-python issue #286 — see
    cwms-overview.md §8) and hit `/levels/{id}/timeseries` directly via
    `cwms.api.get`. The response carries `source_workaround: "issue-286"`
    when the bypass is taken. Every return carries `evaluated_at`.
    """
    at = at or effective_date
    with upstream_call(f"/levels/{level_id}", operation=f"level lookup for {office}/{level_id}"):
        data = ll_api.get_location_level(
            level_id=level_id,
            office_id=office,
            effective_date=effective_date,
            unit=unit,
        )
    payload = require_record(
        data.json if hasattr(data, "json") else data,
        endpoint=f"/levels/{level_id}",
        operation="level lookup",
        field="level_id",
        value=level_id,
    )

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
            "evaluated_at": at.isoformat(),
        }

    if _is_seasonal(payload):
        return _seasonal_workaround(
            level_id=level_id,
            office=office,
            effective_date=effective_date,
            unit=unit,
            level_payload=payload,
            at=at,
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
        "evaluated_at": at.isoformat(),
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


def level_id_of(row: dict[str, Any]) -> str | None:
    """A level row's id, under whichever spelling the payload uses."""
    for key in _LEVEL_ID_KEYS:
        v = row.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _level_entries(payload: Any) -> list[dict[str, Any]] | None:
    """Rows of a levels payload, or None when no known shape matches.

    A bare dict is one level only if it carries a level id; the `{}` that
    cwms-python returns for an empty body is not a level (#152). A real empty
    listing is `{"levels": []}`.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("entries", "items", "values", "levels"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    if level_id_of(payload) is not None:
        return [payload]
    return None


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
    at: datetime | None = None,
) -> dict[str, Any]:
    """Direct-CDA hit on `/levels/{id}/timeseries` to work around issue #286.

    The wrapper's `get_level_as_timeseries` returns wrong values for seasonal
    levels (cwms-python issue #286 — see cwms-overview.md §8). The fix is to
    bypass it and hit the underlying endpoint with the resolved
    `effective-date` (which level revision to use, from `effective_date`) and
    a 1-hour query window starting at `at` (when the threshold is evaluated;
    defaults to `effective_date`).
    """

    at = at or effective_date
    window_end = at + timedelta(hours=1)
    quoted = quote(level_id, safe="")
    params = {
        "office": office,
        "effective-date": effective_date.isoformat(),
        "begin": at.isoformat(),
        "end": window_end.isoformat(),
        "interval": "1Hour",
        "unit": unit,
    }
    endpoint = f"/levels/{level_id}/timeseries"
    try:
        with upstream_call(
            endpoint, operation=f"seasonal-level workaround for {office}/{level_id}"
        ):
            # No leading slash: BaseUrlSession concatenates `api_root` (which already
            # includes `cwms-data/`) with this relative path. A leading slash would
            # bypass the api_root prefix.
            raw = cwms_api.get(f"levels/{quoted}/timeseries", params)
        payload = require_collection(
            raw,
            keys=("values",),
            endpoint=endpoint,
            operation="seasonal-level workaround",
            field="level_id",
            value=level_id,
        )
    except CwmsToolsError as err:
        # Classified like any upstream failure; tag the bypass so the caller
        # can see the request went through the issue-286 workaround.
        err.envelope.source.workaround = "issue-286"
        raise
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
        "evaluated_at": at.isoformat(),
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
    "applicable_revisions",
    "classify",
    "fetch_level_value",
    "list_levels",
    "resolve_applicable_level",
]
