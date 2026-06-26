"""Shared `detail` response shaping for the CLI and MCP surfaces.

Both surfaces wrap the **same** `core/` producers, and both expose the same
`summary`/`full` `detail` toggle. The pruning that toggle performs used to be
re-implemented in two places — `mcp/tools.py` `_shape_*` and inline in each
`cli/commands/*` command — and the copies drifted into defects (#45, #55).

This module is the single home for that shaping: pure
`(payload, detail) -> dict` functions, copy-on-write (never mutating the
producer payload in place). Each surface imports and calls them, then layers on
its own surface-specific concerns:

- MCP: shape → stamp `source` → `Model.model_validate(...)`.
- CLI: shape → `render.emit(...)`.

What stays *out* of this module on purpose: stamping `source`, Pydantic
validation, and pure serialization concerns (null-strip lives in
`core._compact`, float-rounding in `core.rounding`). `Detail` is already a
shared core enum, so this introduces no new presentation coupling.

When you add or change a tool's fields or its `detail` shaping, change it here
once — and extend the CLI↔MCP parity test (`tests/test_cli_mcp_parity.py`) so
the two surfaces cannot silently drift again.
"""

from __future__ import annotations

from typing import Any

from cwms_tools.core.models import Detail

# The triage subset of the Location DTO surfaced in `summary` mode. `full`
# returns every field the producer emitted.
LOCATION_SUMMARY_KEYS: tuple[str, ...] = (
    "office-id",
    "name",
    "location-kind",
    "latitude",
    "longitude",
    "public-name",
    "long-name",
    "horizontal-datum",
    "state-initial",
    "nearest-city",
    "timezone-name",
)


def shape_place_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Shape a place response (`search_places` / `describe_place`).

    Summary mode prunes a `location` record to `LOCATION_SUMMARY_KEYS` and drops
    the verbose `raw` blob from each search `result`. Each branch is a no-op when
    its key is absent, so one function serves both place tools.
    """
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    loc = pruned.get("location")
    if isinstance(loc, dict):
        pruned["location"] = {k: loc.get(k) for k in LOCATION_SUMMARY_KEYS if k in loc}
    results = pruned.get("results")
    if isinstance(results, list):
        pruned["results"] = [
            {k: v for k, v in r.items() if k != "raw"} for r in results if isinstance(r, dict)
        ]
    return pruned


def shape_value_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Shape a `get_value` response; summary drops `level_id`/`source_workaround`
    internals from each active threshold."""
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    if isinstance(pruned.get("thresholds_active"), list):
        pruned["thresholds_active"] = [
            {k: v for k, v in t.items() if k not in {"level_id", "source_workaround"}}
            for t in pruned["thresholds_active"]
        ]
    return pruned


def shape_history_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Shape a `get_history` response; summary drops the per-point `quality` flag."""
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    if isinstance(pruned.get("values"), list):
        pruned["values"] = [
            {k: v for k, v in row.items() if k != "quality"} for row in pruned["values"]
        ]
    return pruned


def shape_profile_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Shape a `get_profile` response; summary drops the chatty per-sensor `ts_id`
    (`depth` + value stay)."""
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    if isinstance(pruned.get("profile"), list):
        pruned["profile"] = [
            {k: v for k, v in sensor.items() if k != "ts_id"} for sensor in pruned["profile"]
        ]
    return pruned


def shape_publishers_detail(payload: dict[str, Any], detail: Detail) -> dict[str, Any]:
    """Shape a `publishers_for_parameter` response; summary drops the internal
    `_observed_publishers_by_office` diagnostic (#55)."""
    if detail is Detail.FULL:
        return dict(payload)
    pruned = dict(payload)
    pruned.pop("_observed_publishers_by_office", None)
    return pruned


__all__ = [
    "LOCATION_SUMMARY_KEYS",
    "shape_history_detail",
    "shape_place_detail",
    "shape_profile_detail",
    "shape_publishers_detail",
    "shape_value_detail",
]
