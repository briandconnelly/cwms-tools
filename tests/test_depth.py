"""Tests for depth-tag parsing (issue #27)."""

from __future__ import annotations

import pytest

from cwms_tools.core.depth import depth_sort_key, parse_depth


@pytest.mark.parametrize(
    ("name", "value", "unit"),
    [
        ("GWLW_S1-D3,0ft", 3.0, "ft"),
        ("GWLW_S1-D13,0ft", 13.0, "ft"),
        ("GWLW_S1-D36,0ft", 36.0, "ft"),
        ("UBLW_S1-D21,0ft", 21.0, "ft"),
        ("BECR-D042,5m", 42.5, "m"),
        ("FOO-D3m", 3.0, "m"),
    ],
)
def test_parse_depth_extracts_value_and_unit(name, value, unit) -> None:
    depth = parse_depth(name)
    assert depth == {"value": pytest.approx(value), "unit": unit}


@pytest.mark.parametrize(
    "name",
    [
        "GWLW_S1",
        "FOSS",
        "BON-U1",
        "FTPK-bl_7000",
        "GWLW_S1-Lock01",
        # The depth tag is a SUFFIX: a `-D…ft` that isn't at the end must not match.
        "GWLW_S1-D3,0ft-RAW",
        "X-D5m-extra",
    ],
)
def test_parse_depth_returns_none_for_non_depth_ids(name) -> None:
    assert parse_depth(name) is None


def test_depth_sort_key_orders_shallow_to_deep() -> None:
    names = ["GWLW_S1-D36,0ft", "GWLW_S1-D3,0ft", "GWLW_S1-D25,0ft", "GWLW_S1-D13,0ft"]
    assert sorted(names, key=depth_sort_key) == [
        "GWLW_S1-D3,0ft",
        "GWLW_S1-D13,0ft",
        "GWLW_S1-D25,0ft",
        "GWLW_S1-D36,0ft",
    ]


def test_depth_sort_key_normalizes_mixed_units() -> None:
    # 1 m (~3.28 ft) is deeper than 2 ft but shallower than 10 ft.
    names = ["X-D10,0ft", "X-D1m", "X-D2,0ft"]
    assert sorted(names, key=depth_sort_key) == ["X-D2,0ft", "X-D1m", "X-D10,0ft"]


def test_depth_sort_key_puts_non_depth_last() -> None:
    assert sorted(["GWLW_S1-D5,0ft", "GWLW_S1"], key=depth_sort_key) == [
        "GWLW_S1-D5,0ft",
        "GWLW_S1",
    ]
