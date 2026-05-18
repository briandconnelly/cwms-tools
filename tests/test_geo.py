"""Tests for client-side geographic filtering."""

from __future__ import annotations

import math

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
