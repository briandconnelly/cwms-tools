"""Office discovery — primary source `cwms.api.get("offices")` with caching.

Backs the optional-`office` mode of `cwms_search_places`: when an agent asks
"what is the water temperature at Fremont Bridge?" without naming an office,
the tool fans out across offices that are already cached this session.
This module supplies the office list both upstream-fresh and as a degraded
fallback for the "no offices cached, no upstream" cold start.

The fanout itself is in `core/places.py:search_places` and mirrors the
budgeted pattern from `core/publishers_index.py` so we never expand to the
full ~68-office surface implicitly.
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


def list_office_ids(*, use_cache: bool = True) -> tuple[list[str], bool]:
    """Return `(office_ids, used_fallback)`.

    Primary source is `cwms.api.get("offices")` via the existing
    `"offices"` cache namespace (7-day TTL, see `core/cache.py`). Returns
    the documented degraded fallback when upstream fails or returns an
    empty / unrecognized payload; `used_fallback` is true in that case so
    the caller can surface `partial: true` to the agent.
    """
    cache = get_cache()
    cfg = current_config()
    key = build_cache_key("offices", "all", api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(key)
        if isinstance(hit, list) and hit:
            return [str(o) for o in hit], False
    try:
        raw = cwms_api.get("offices")
    except Exception:
        return sorted(_FALLBACK_OFFICES), True
    ids = _parse_office_ids(raw)
    if not ids:
        return sorted(_FALLBACK_OFFICES), True
    ids = sorted(set(ids))
    cache.set(key, ids, ttl=cache.ttl_for("offices"))
    return ids, False


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


def _parse_office_ids(raw: Any) -> list[str]:
    """Tolerate the common CDA shapes for the offices payload."""
    if isinstance(raw, list):
        items: list[Any] = raw
    elif isinstance(raw, dict):
        candidate: Any = raw.get("offices") or raw.get("entries") or raw.get("items") or []
        items = candidate if isinstance(candidate, list) else []
    else:
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            v = it.get("office-id") or it.get("id") or it.get("name")
            if isinstance(v, str):
                out.append(v)
    return out


__all__ = ["cached_offices_for_locations", "list_office_ids"]
