"""`cwms_publishers_for_parameter` — bounded-scope publisher-by-parameter index.

The cwms-overview.md §9.8 task ("which publishers report on parameter X?")
has no native CDA index. This module answers it from cached `ts_catalog`
data plus a bounded lazy backfill of the requested offices — it never
fans out to all 68 offices in a single call. The response carries explicit
`coverage` metadata so the agent can see which offices contributed and
which were skipped.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from cwms_tools.core import catalog, publishers
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.concurrency import MAX_WORKERS
from cwms_tools.core.errors import RepairHint
from cwms_tools.core.session import current_config


def _budget() -> int:
    """How many offices we will index per call. ceil(MAX_WORKERS / 2), min 1."""
    return max(1, math.ceil(MAX_WORKERS / 2))


def _cached_offices() -> set[str]:
    """Best-effort: which office IDs already have ts_catalog cache entries.

    diskcache exposes iteration over keys; we filter to our ts_catalog
    namespace prefix. The lookup is O(cache size), bounded by our own keyspace.
    """
    cache = get_cache()
    cfg = current_config()
    offices: set[str] = set()
    try:
        for raw_key in cache._l2.iterkeys():
            if not isinstance(raw_key, str) or not raw_key.startswith("ts_catalog:"):
                continue
            # We can't easily reverse the SHA-256 hash to the office id, so
            # instead match against a precomputed key for every known office.
            # For now, return all NW + likely-active offices as the candidate
            # set; the actual cache hits are validated in `_office_has_cache`.
            offices.update(_KNOWN_LIKELY_OFFICES)
            break
    except Exception:  # pragma: no cover - diskcache iter failure is non-fatal
        pass
    cached: set[str] = set()
    for office in offices:
        key = build_cache_key("ts_catalog", office, "", api_root=cfg.api_root)
        if cache.get(key) is not None:
            cached.add(office)
    return cached


#: Heuristic candidate set for the "implicit all-offices" lookup. We never
#: fetch any of these eagerly — they are *only* sampled when the caller does
#: not name an office. cwms-overview.md §6.1 office list.
_KNOWN_LIKELY_OFFICES = (
    "NWDM",
    "NWDP",
    "MVS",
    "MVK",
    "MVM",
    "MVN",
    "MVP",
    "MVR",
    "SWT",
    "SWL",
    "SWG",
    "SWF",
    "LRB",
    "LRC",
    "LRE",
    "LRH",
    "LRL",
    "LRN",
    "LRP",
)


def publishers_for_parameter(
    parameter: str,
    *,
    offices: list[str] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return publishers reporting parameter X across the requested offices.

    `offices=None` means "the offices already in cache" — we never expand to
    the full 68-office surface implicitly. Pass an explicit list to widen.

    The per-call budget caps how many *new* (uncached) offices we will fetch;
    additional offices are listed under `offices_skipped_for_budget` with a
    repair hint that points back at this tool with that list as the next
    `offices` argument.
    """
    requested = list(offices) if offices is not None else sorted(_cached_offices())
    requested_unique: list[str] = []
    seen: set[str] = set()
    for o in requested:
        if o not in seen:
            seen.add(o)
            requested_unique.append(o)
    requested = requested_unique

    indexed: list[str] = []
    skipped: list[str] = []
    by_publisher: dict[str, list[str]] = defaultdict(list)
    freshness: dict[str, str | None] = {}

    budget_remaining = _budget()
    for office in requested:
        was_cached = _office_has_cache(office)
        if was_cached or budget_remaining > 0:
            try:
                tsids = _gather_ts_ids(office, use_cache=use_cache)
            except Exception:
                skipped.append(office)
                continue
            if not was_cached:
                budget_remaining -= 1
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

    complete = not skipped
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

    repair = None
    if skipped:
        repair = RepairHint(
            tool="cwms_publishers_for_parameter",
            args={"parameter": parameter, "offices": skipped},
        ).model_dump(mode="json")

    return {
        "parameter": parameter,
        "publishers": publisher_rows,
        "publisher_count": len(publisher_rows),
        "ts_count": sum(len(locs) for locs in by_publisher.values()),
        "coverage": {
            "offices_requested": requested,
            "offices_indexed": indexed,
            "offices_skipped_for_budget": skipped,
            "complete": complete,
        },
        "repair": repair,
        "_observed_publishers_by_office": {
            office: sorted(Counter(by_publisher)) for office in indexed
        },
    }


def _office_has_cache(office: str) -> bool:
    """Cheap check: does the ts_catalog cache already hold this office?"""
    cache = get_cache()
    cfg = current_config()
    key = build_cache_key("ts_catalog", office, "", api_root=cfg.api_root)
    return cache.get(key) is not None


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
