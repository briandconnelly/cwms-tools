"""`cwms_publishers_for_parameter` — bounded-scope publisher-by-parameter index.

The cwms-overview.md §9.8 task ("which publishers report on parameter X?")
has no native CDA index. This module answers it from cached `ts_catalog`
data plus a bounded lazy backfill of the requested offices — it never
fans out to all 68 offices in a single call. The response carries explicit
`coverage` metadata so the agent can see which offices contributed and
which were skipped.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from cwms_tools.core import catalog, concurrency, publishers
from cwms_tools.core import offices as offices_mod
from cwms_tools.core.errors import CwmsToolsError, RepairHint


def _budget() -> int:
    """How many offices we will index per call: the shared fan-out cap.

    Delegates to `concurrency.fanout_budget()` so this and `search_places`
    stay locked to one identical value; kept as a monkeypatch seam for tests.
    """
    return concurrency.fanout_budget()


def _cached_offices() -> set[str]:
    """Offices whose unfiltered ts_catalog is already on disk.

    diskcache keys are hashed, so we cannot enumerate them; probe the curated
    candidate set (`offices_mod.discovery_office_candidates()`) instead. Only
    offices that are *actually* cached come back — nothing is fetched here.
    """
    return {o for o in offices_mod.discovery_office_candidates() if _office_has_cache(o)}


def publishers_for_parameter(
    parameter: str,
    *,
    offices: list[str] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return publishers reporting parameter X across the requested offices.

    `offices=None` means "the offices already in cache" — we never expand to
    the full 68-office surface implicitly. Pass an explicit list to widen.
    With `offices` omitted, `complete` is true only when every curated office
    (`offices.discovery_office_candidates()`) was indexed; otherwise `repair`
    names the curated offices not yet indexed — on a cold cache, all of them
    (#164). With explicit `offices`, `complete` means "all requested offices
    were indexed".

    The per-call budget caps how many *new* (uncached) offices we will fetch;
    additional offices are listed under `offices_skipped_for_budget` with a
    repair hint that points back at this tool with that list as the next
    `offices` argument.
    """
    requested = list(offices) if offices is not None else sorted(_cached_offices())
    requested_unique: list[str] = []
    error_skipped: list[str] = []
    for requested_office in requested:
        try:
            canonical = offices_mod.require_known_office(requested_office)
        except CwmsToolsError:
            error_skipped.append(requested_office)
            continue
        if canonical not in requested_unique:
            requested_unique.append(canonical)
    requested = requested_unique

    indexed: list[str] = []
    skipped: list[str] = []
    by_publisher: dict[str, list[str]] = defaultdict(list)
    freshness: dict[str, str | None] = {}

    budget_remaining = _budget()
    for office in requested:
        was_cached = use_cache and _office_has_cache(office)
        if was_cached or budget_remaining > 0:
            # An uncached office consumes the budget on *attempt*, not just on
            # success: the fetch hits the upstream regardless of outcome, so a
            # run of erroring offices can't blow past the per-call fetch cap.
            if not was_cached:
                budget_remaining -= 1
            try:
                tsids = _gather_ts_ids(office, use_cache=use_cache)
            except CwmsToolsError:
                # An office whose catalog fetch failed (e.g. upstream_error,
                # rate_limited, ghost_office) is recorded separately from
                # budget-skipped offices so the caller can tell "hit the budget,
                # re-run with these" from "these errored". Unexpected non-
                # CwmsToolsError exceptions propagate so genuine bugs surface.
                error_skipped.append(office)
                continue
            indexed.append(office)
            for tsid in tsids:
                parts = publishers.parse_ts_id(tsid)
                if parts is None or parts.parameter != parameter:
                    continue
                by_publisher[parts.version].append(office + "/" + parts.location)
                # Freshness left null in the index summary; per-location freshness
                # is available via `cwms_describe_place`.
                freshness.setdefault(parts.version, None)
        else:
            skipped.append(office)

    curated_uncached = [o for o in offices_mod.discovery_office_candidates() if o not in indexed]
    if offices is None:
        complete = not curated_uncached and not skipped and not error_skipped
    else:
        complete = bool(indexed) and not skipped and not error_skipped
    publisher_rows = [
        {
            "publisher": pub,
            "rank": publishers.publisher_rank(pub),
            "locations_known": len(set(locs)),
            "freshness": freshness.get(pub),
        }
        for pub, locs in by_publisher.items()
    ]
    publisher_rows.sort(key=lambda r: (-r["rank"], r["publisher"]))

    repair = _repair_hint(
        parameter, explicit=offices is not None, skipped=skipped, curated_uncached=curated_uncached
    )

    return {
        "parameter": parameter,
        "publishers": publisher_rows,
        "publisher_count": len(publisher_rows),
        "ts_count": sum(len(locs) for locs in by_publisher.values()),
        "coverage": {
            "offices_requested": requested,
            "offices_indexed": indexed,
            "offices_skipped_for_budget": skipped,
            "offices_error_skipped": error_skipped,
            "complete": complete,
        },
        "repair": repair,
        "_observed_publishers_by_office": {
            office: sorted(Counter(by_publisher)) for office in indexed
        },
    }


def _repair_hint(
    parameter: str, *, explicit: bool, skipped: list[str], curated_uncached: list[str]
) -> dict[str, Any] | None:
    """The `repair` for a coverage gap: budget skips first, else the curated rest.

    With `offices` omitted (#164) only the cached slice was indexed — on a cold
    cache, nothing — so name the curated offices not yet indexed, mirroring
    `cwms_search_places`'s starting set.
    """
    if skipped:
        hint = RepairHint(
            next_step="retry_skipped_offices",
            tool="cwms_publishers_for_parameter",
            arguments={
                "parameter": parameter,
                # Implicit scope: the curated offices never indexed ride along.
                "offices": list(dict.fromkeys([*skipped, *([] if explicit else curated_uncached)])),
            },
        )
    elif not explicit and curated_uncached:
        hint = RepairHint(
            next_step="index_curated_offices",
            tool="cwms_publishers_for_parameter",
            arguments={"parameter": parameter, "offices": curated_uncached},
        )
    else:
        return None
    return hint.model_dump(mode="json")


def _office_has_cache(office: str) -> bool:
    """Cheap check: is the office's unfiltered ts_catalog already on disk?"""
    return catalog.ts_catalog_cached(office)


def _gather_ts_ids(office: str, *, use_cache: bool) -> list[str]:
    """Return all distinct ts_ids in an office's ts_catalog (cached)."""
    payload = catalog.get_timeseries_catalog(office, use_cache=use_cache)
    rows = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("timeseries", []) if isinstance(payload, dict) else []
    ts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tsid = row.get("name") or row.get("time-series-id") or row.get("timeseries-id")
        if isinstance(tsid, str):
            ts.append(tsid)
    return ts


__all__ = ["publishers_for_parameter"]
