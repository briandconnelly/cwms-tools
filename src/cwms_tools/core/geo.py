"""Client-side geographic filtering.

CDA stores latitude/longitude per location record but does not expose a
bounding-box or radius search endpoint. This module provides the math so
callers can filter the cached catalog client-side, and detect co-located
variants (records within a small radius of the same coordinates).

All math is on WGS84-style decimal degrees. We do not attempt geodetic
correctness across datums — see cwms-overview.md §4.2 on datums; the
caller is expected to preserve `horizontalDatum` separately so spatial
joins remain interpretable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6371008.8  # mean radius, in meters


@dataclass(frozen=True)
class BBox:
    """Geographic bounding box in decimal degrees (lat, lon)."""

    south: float
    west: float
    north: float
    east: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east


@dataclass(frozen=True)
class GeoPoint:
    """A location keyed by `(office_id, name)` for co-location grouping."""

    office_id: str
    name: str
    latitude: float
    longitude: float


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def filter_by_bbox(points: list[GeoPoint], bbox: BBox) -> list[GeoPoint]:
    """Return only the points inside `bbox`."""
    return [p for p in points if bbox.contains(p.latitude, p.longitude)]


def co_located(
    target: GeoPoint,
    candidates: list[GeoPoint],
    *,
    radius_m: float = 100.0,
) -> list[GeoPoint]:
    """Return the candidates within `radius_m` of `target`, excluding the target itself.

    Default radius of 100 m matches the cwms-overview.md §6.3 heuristic for
    detecting co-located variants under different ids.
    """
    out: list[GeoPoint] = []
    for c in candidates:
        if c.office_id == target.office_id and c.name == target.name:
            continue
        if haversine_m(target.latitude, target.longitude, c.latitude, c.longitude) <= radius_m:
            out.append(c)
    return out


__all__ = ["EARTH_RADIUS_M", "BBox", "GeoPoint", "co_located", "filter_by_bbox", "haversine_m"]
