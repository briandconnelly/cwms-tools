"""Name resolution + co-located variant grouping over the locations catalog.

Wraps `cwms.locations.physical_locations.get_location` and the enriched
catalog browse in `core.catalog`. Surfaces NW-stub repair hints and the
canonical PlaceSummary shape used by `cwms_search_places` /
`cwms_describe_place`.
"""

from __future__ import annotations

from typing import Any

from cwms.locations.physical_locations import get_location

from cwms_tools.core import catalog
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

# NW Division district stubs — publish no data in CDA. Documented in
# cwms-overview.md §6.1. Mirror the short-circuit from `core.catalog` so
# single-location reads (place describe, place parameters) surface the
# same agent-friendly repair hint instead of a database-internals 404.
_NW_STUBS: frozenset[str] = frozenset({"NWO", "NWK", "NWS", "NWP", "NWW"})
_NW_REPAIR_TARGETS: dict[str, str] = {
    "NWO": "NWDM",
    "NWK": "NWDM",
    "NWS": "NWDP",
    "NWP": "NWDP",
    "NWW": "NWDP",
}


def _ghost_office_error(office_id: str) -> CwmsToolsError:

    target = _NW_REPAIR_TARGETS.get(office_id, "NWDM")
    return CwmsToolsError.of(
        ErrorCode.GHOST_OFFICE,
        f"Office {office_id} publishes no operational data; use the regional rollup.",
        field="office_id",
        offending_value=office_id,
        hint=(
            "NW Division districts (NWO, NWK, NWS, NWP, NWW) are catalog stubs. "
            "Use NWDM (Missouri) or NWDP (Pacific NW) instead."
        ),
        repair=RepairHint(tool="cwms_search_places", args={"query": "", "office": target}),
    )


def search(
    office_id: str,
    query: str,
    *,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return enriched locations whose names match `query` in `office_id`."""
    return catalog.enrich_locations(office_id, like=query, use_cache=use_cache)


def get_one(office_id: str, name: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Return a single Location's raw payload from cwms-python."""
    if office_id in _NW_STUBS:
        raise _ghost_office_error(office_id)
    cache = catalog.get_cache()
    cfg = catalog.current_config()
    key = catalog.build_cache_key(
        "location_catalog", office_id, "single", name, api_root=cfg.api_root
    )
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    try:
        data = get_location(location_id=name, office_id=office_id)
    except Exception as exc:  # pragma: no cover - upstream wraps as ApiError
        raise CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            f"Location {office_id}/{name} not found upstream: {exc}",
            field="name",
            offending_value=name,
            endpoints_called=[f"/locations/{name}"],
        ) from exc
    payload = data.json if hasattr(data, "json") else data
    cache.set(key, payload, ttl=cache.ttl_for("location_catalog"))
    return payload


__all__ = ["get_one", "search"]
