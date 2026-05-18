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
from cwms_tools.core.errors import CwmsToolsError, ErrorCode


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
