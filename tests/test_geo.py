"""Tests for client-side geographic filtering."""

from __future__ import annotations

import math
import random

import pytest

from cwms_tools.core import geo


def test_haversine_zero_distance() -> None:
    d = geo.haversine_m(45.0, -100.0, 45.0, -100.0)
    assert d == 0.0


def test_haversine_known_distance() -> None:
    # Approx 111 km per degree of latitude at the equator.
    d = geo.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert math.isclose(d, 111195, rel_tol=1e-3)


def test_bbox_contains() -> None:
    bbox = geo.BBox(south=45.0, west=-110.0, north=48.0, east=-105.0)
    assert bbox.contains(46.0, -107.0)
    assert not bbox.contains(50.0, -107.0)
    assert not bbox.contains(46.0, -100.0)


def test_filter_by_bbox() -> None:
    pts = [
        geo.GeoPoint("NWDM", "FTPK", 47.99, -106.41),
        geo.GeoPoint("NWDM", "OAHE", 44.45, -100.39),
        geo.GeoPoint("SWT", "FOSS", 35.55, -98.97),
    ]
    bbox = geo.BBox(south=45.0, west=-110.0, north=49.0, east=-100.0)
    inside = geo.filter_by_bbox(pts, bbox)
    assert [p.name for p in inside] == ["FTPK"]


def test_co_located_excludes_self_and_far_points() -> None:
    target = geo.GeoPoint("NWO", "BECR", 39.633, -105.293)
    candidates = [
        geo.GeoPoint("NWO", "BECR", 39.633, -105.293),  # self
        geo.GeoPoint("NWDM", "BECR", 39.6331, -105.2931),  # ~10 m
        geo.GeoPoint("SWT", "FOSS", 35.55, -98.97),  # far away
    ]
    siblings = geo.co_located(target, candidates, radius_m=100.0)
    assert [c.office_id for c in siblings] == ["NWDM"]


def test_co_located_radius_is_configurable() -> None:
    target = geo.GeoPoint("X", "A", 0.0, 0.0)
    a_far = geo.GeoPoint("X", "B", 0.01, 0.0)  # ~1.1 km
    near = geo.GeoPoint("X", "C", 0.0001, 0.0)  # ~11 m
    siblings = geo.co_located(target, [a_far, near], radius_m=50.0)
    assert [c.name for c in siblings] == ["C"]
    siblings = geo.co_located(target, [a_far, near], radius_m=2000.0)
    assert {c.name for c in siblings} == {"B", "C"}


def _brute(points, radius_m):
    return {(p.office_id, p.name): geo.co_located(p, points, radius_m=radius_m) for p in points}


def test_co_located_index_matches_brute_force_on_random_cluster() -> None:
    rng = random.Random(1234)
    pts = [
        geo.GeoPoint(
            "X", f"P{i}", 48.0 + rng.uniform(-0.003, 0.003), -100.0 + rng.uniform(-0.005, 0.005)
        )
        for i in range(300)
    ]
    assert geo.co_located_index(pts, radius_m=100.0) == _brute(pts, 100.0)


def test_co_located_index_catches_pair_that_straddles_degree_grid_cells() -> None:
    """Codex counterexample for a 0.001-degree grid: 89 m apart at 48 N but two
    longitude cells apart. The ECEF hash must still pair them."""
    a = geo.GeoPoint("X", "A", 48.0, -100.0001)
    b = geo.GeoPoint("X", "B", 48.0, -99.9989)
    assert geo.haversine_m(a.latitude, a.longitude, b.latitude, b.longitude) < 100.0
    idx = geo.co_located_index([a, b], radius_m=100.0)
    assert [p.name for p in idx[("X", "A")]] == ["B"]
    assert [p.name for p in idx[("X", "B")]] == ["A"]


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(0.0, 0.0), (0.0, 179.9999), (0.0, -179.9999), (89.9, 10.0), (-89.9, -10.0)],
)
def test_co_located_index_matches_brute_force_at_antimeridian_and_poles(lat, lon) -> None:
    pts = [
        geo.GeoPoint("X", "A", lat, lon),
        geo.GeoPoint("X", "B", lat + 0.0005, lon),
        geo.GeoPoint("X", "C", lat, lon - 0.0004 if lon > 0 else lon + 0.0004),
        geo.GeoPoint("X", "D", lat - 0.05, lon),
    ]
    assert geo.co_located_index(pts, radius_m=100.0) == _brute(pts, 100.0)


def test_co_located_index_preserves_input_order_and_excludes_self() -> None:
    pts = [
        geo.GeoPoint("NWDM", "BECR", 39.6331, -105.2931),
        geo.GeoPoint("NWO", "BECR", 39.633, -105.293),
        geo.GeoPoint("NWD", "BECR", 39.6332, -105.2932),
        geo.GeoPoint("SWT", "FOSS", 35.55, -98.97),
    ]
    idx = geo.co_located_index(pts)
    assert [p.office_id for p in idx[("NWO", "BECR")]] == ["NWDM", "NWD"]
    assert idx[("SWT", "FOSS")] == []


def test_co_located_index_skips_rows_without_coordinates() -> None:
    """Review focus #2: callers filter None coordinates before building GeoPoints;
    the index must accept an empty list and a single point without error."""
    assert geo.co_located_index([]) == {}
    only = geo.GeoPoint("X", "A", 1.0, 1.0)
    assert geo.co_located_index([only]) == {("X", "A"): []}
