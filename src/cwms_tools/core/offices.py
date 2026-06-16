"""Office discovery — primary source `cwms.api.get("offices")` with caching.

Backs two surfaces:

- the `cwms://offices` MCP resource (and its office-code discovery role),
  via `list_offices()` which returns full office records; and
- the optional-`office` mode of `cwms_search_places`: when an agent asks
  "what is the water temperature at Fremont Bridge?" without naming an
  office, the tool fans out across offices already cached this session.

This module supplies the office list both upstream-fresh and as a degraded
fallback for the "no offices cached, no upstream" cold start. The fanout
itself is in `core/places.py:search_places` and mirrors the budgeted
pattern from `core/publishers_index.py` so we never expand to the full
~68-office surface implicitly.
"""

from __future__ import annotations

from typing import Any

from cwms import api as cwms_api

from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.session import current_config

# Documented degraded fallback. Used only when the upstream offices fetch
# fails — never as the default scope for fanout. Mirrors the candidate set
# in `core/publishers_index.py` so the fallback names a realistic, mostly
# data-bearing slice rather than the full theoretical office surface.
_FALLBACK_OFFICES: tuple[str, ...] = (
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

# CDA `/offices` `type` codes → human label. Unknown codes pass through as
# themselves (see `_type_label`) so a new upstream code degrades gracefully
# rather than vanishing. Codes verified live 2026-06-10; see cwms-overview.md §4.1.
_TYPE_LABELS: dict[str, str] = {
    "HQ": "corps headquarters",
    "MSC": "division headquarters",
    "MSCR": "division regional",
    "DIS": "district",
    "FOA": "field operating activity",
    "UNK": "unknown",
}


def _type_label(code: str) -> str:
    return _TYPE_LABELS.get(code, code)


def list_offices(*, use_cache: bool = True) -> tuple[list[dict[str, Any]], bool]:
    """Return `(office_records, used_fallback)`.

    Each record is a normalized dict with `name` and, when upstream
    supplies them, `long_name`, `type` (raw CDA code), `type_label`
    (human label), and `reports_to`. Records are sorted by `name`.

    Primary source is `cwms.api.get("offices")` cached under the `"offices"`
    namespace (7-day TTL, see `core/cache.py`). On upstream failure or an
    empty/unrecognized payload, returns the documented degraded fallback as
    name-only records with `used_fallback=True`, so a caller can surface
    `partial: true` to the agent.
    """
    cache = get_cache()
    cfg = current_config()
    key = build_cache_key("offices", "records", api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(key)
        if isinstance(hit, list) and hit:
            return [dict(r) for r in hit], False
    try:
        raw = cwms_api.get("offices")
    except Exception:
        return _fallback_records(), True
    records = _parse_office_records(raw)
    if not records:
        return _fallback_records(), True
    records.sort(key=lambda r: r["name"])
    cache.set(key, records, ttl=cache.ttl_for("offices"))
    return [dict(r) for r in records], False


def list_office_ids(*, use_cache: bool = True) -> tuple[list[str], bool]:
    """Return `(office_ids, used_fallback)` — names only, derived from `list_offices`."""
    records, used_fallback = list_offices(use_cache=use_cache)
    return [r["name"] for r in records], used_fallback


def cached_offices_for_locations() -> list[str]:
    """Return office IDs whose unfiltered locations catalog is already cached.

    Used as the default fanout scope when an agent omits `office` from
    `cwms_search_places`. The check probes the cache for the unfiltered
    `location_catalog` key per candidate office; this is cheap because
    `cache.get` short-circuits on miss.

    Candidate set is the same heuristic slice the fallback uses, since
    diskcache keys are SHA-256 hashed and we cannot iterate them directly.
    """
    cache = get_cache()
    cfg = current_config()
    cached: list[str] = []
    for office in _FALLBACK_OFFICES:
        key = build_cache_key("location_catalog", office, "", api_root=cfg.api_root)
        if cache.get(key) is not None:
            cached.append(office)
    return sorted(cached)


def discovery_office_candidates() -> list[str]:
    """The curated data-bearing office set used to seed name-first discovery.

    When an agent searches by bare name with no office in scope, this is the
    concrete list `cwms_search_places` names in its `repair_hint` (issue #24):
    the same heuristic slice `cached_offices_for_locations` probes — regional
    rollups (NWDM/NWDP) plus the district-publishing offices — so the agent
    gets a realistic, mostly data-bearing starting set rather than the full
    ~68-office surface.
    """
    return sorted(_FALLBACK_OFFICES)


def _fallback_records() -> list[dict[str, Any]]:
    """Name-only records for the degraded fallback path (no upstream metadata)."""
    return [{"name": name} for name in sorted(_FALLBACK_OFFICES)]


def _parse_office_records(raw: Any) -> list[dict[str, Any]]:
    """Tolerate the common CDA shapes for the offices payload.

    Live CDA returns a JSON array of `{name, long-name, type, reports-to}`;
    some wrappers nest it under `offices`/`entries`/`items`.
    """
    if isinstance(raw, list):
        items: list[Any] = raw
    elif isinstance(raw, dict):
        candidate: Any = raw.get("offices") or raw.get("entries") or raw.get("items") or []
        items = candidate if isinstance(candidate, list) else []
    else:
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            out.append({"name": it})
            continue
        if not isinstance(it, dict):
            continue
        name = it.get("office-id") or it.get("id") or it.get("name")
        if not isinstance(name, str):
            continue
        record: dict[str, Any] = {"name": name}
        long_name = it.get("long-name") or it.get("longName")
        if isinstance(long_name, str):
            record["long_name"] = long_name
        code = it.get("type")
        if isinstance(code, str) and code:
            record["type"] = code
            record["type_label"] = _type_label(code)
        reports_to = it.get("reports-to") or it.get("reportsTo")
        if isinstance(reports_to, str):
            record["reports_to"] = reports_to
        out.append(record)
    return out


__all__ = [
    "cached_offices_for_locations",
    "discovery_office_candidates",
    "list_office_ids",
    "list_offices",
]
