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


def first_missing_bbox_field(
    south: float | None, west: float | None, north: float | None, east: float | None
) -> str | None:
    """Name of the first unset corner (canonical south/west/north/east order).

    A bounding box requires all four corners or none — when 1-3 are
    provided, this names a real, retryable argument for the caller's error
    `field` (#68), rather than a synthetic "bbox" that isn't an actual
    parameter of either surface (MCP tool params / CLI flags are the four
    separate corners, not a combined "bbox" object).
    """
    for name, value in (("south", south), ("west", west), ("north", north), ("east", east)):
        if value is None:
            return name
    return None


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


def _ecef_m(lat: float, lon: float) -> tuple[float, float, float]:
    """Earth-centered, Earth-fixed coordinates in meters on the mean sphere."""
    phi, lam = math.radians(lat), math.radians(lon)
    cos_phi = math.cos(phi)
    return (
        EARTH_RADIUS_M * cos_phi * math.cos(lam),
        EARTH_RADIUS_M * cos_phi * math.sin(lam),
        EARTH_RADIUS_M * math.sin(phi),
    )


def co_located_index(
    points: list[GeoPoint],
    *,
    radius_m: float = 100.0,
) -> dict[tuple[str, str], list[GeoPoint]]:
    """Every point's co-located siblings in one pass: `{(office_id, name): [...]}`.

    Equivalent to `co_located(p, points, radius_m=radius_m)` for each `p` —
    same members, same (input) order — but O(N + candidate pairs) instead of
    O(N²) (#169: a 10 k-row office took 160 s under the pairwise form).

    Points are hashed into a cubic grid over ECEF coordinates with cell size
    `radius_m` (floored at 1e-9 m so a zero radius still buckets and the exact
    haversine test decides). The chord between two points is never longer than their
    great-circle distance, so any pair within `radius_m` along the surface is
    within `radius_m` on every axis and lands in the same or an adjacent cell.
    Only those 27 cells are compared, then the exact haversine test runs as
    before. A degree grid was rejected because its longitude cells shrink
    with latitude; this hash has no latitude, antimeridian, or pole cases.
    """
    cell = max(float(radius_m), 1e-9)
    keys: list[tuple[int, int, int]] = []
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for i, p in enumerate(points):
        x, y, z = _ecef_m(p.latitude, p.longitude)
        key = (math.floor(x / cell), math.floor(y / cell), math.floor(z / cell))
        keys.append(key)
        buckets.setdefault(key, []).append(i)

    out: dict[tuple[str, str], list[GeoPoint]] = {}
    for i, p in enumerate(points):
        kx, ky, kz = keys[i]
        hits: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in buckets.get((kx + dx, ky + dy, kz + dz), ()):
                        if j == i:
                            continue
                        q = points[j]
                        if q.office_id == p.office_id and q.name == p.name:
                            continue
                        if (
                            haversine_m(p.latitude, p.longitude, q.latitude, q.longitude)
                            <= radius_m
                        ):
                            hits.append(j)
        hits.sort()  # input order, matching `co_located`
        out[(p.office_id, p.name)] = [points[j] for j in hits]
    return out


__all__ = [
    "EARTH_RADIUS_M",
    "BBox",
    "GeoPoint",
    "co_located",
    "co_located_index",
    "filter_by_bbox",
    "haversine_m",
]
