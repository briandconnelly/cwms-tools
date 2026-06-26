"""Tests for serialization-boundary float rounding (issue #45)."""

from __future__ import annotations

import json
import math

import pytest

from cwms_tools.core.rounding import DEFAULT_SIG_FIGS, round_floats


def test_strips_unit_conversion_noise() -> None:
    # The exact values from issue #45 — °C↔°F conversion artifacts.
    assert round_floats(68.55000000000001) == 68.55
    assert round_floats(68.20000000000002) == 68.2
    assert round_floats(65.80000000000001) == 65.8
    assert round_floats(69.80000000000001) == 69.8


def test_significant_figures_scale_with_magnitude() -> None:
    # Six sig figs, not six decimals: a flow in the thousands keeps whole-cfs
    # resolution while a temperature near 20 keeps four decimals.
    assert round_floats(20.305555555555557) == 20.3056
    assert round_floats(1234.56789) == 1234.57
    assert round_floats(1234567.89) == 1234570.0
    assert round_floats(0.001234567) == 0.00123457


def test_leaves_non_floats_untouched() -> None:
    assert round_floats(None) is None
    assert round_floats(42) == 42
    assert round_floats("68.55000000000001") == "68.55000000000001"
    assert round_floats("2026-05-17T18:00:00Z") == "2026-05-17T18:00:00Z"


def test_bool_is_not_rounded() -> None:
    # bool is an int subclass; it must survive identically, not become 1.0/0.0.
    assert round_floats(True) is True
    assert round_floats(False) is False
    assert round_floats({"active": True}) == {"active": True}


def test_recurses_through_dicts_and_lists() -> None:
    payload = {
        "value": 68.55000000000001,
        "values": [
            {"value": 20.305555555555557, "timestamp": "t1"},
            {"value": 65.80000000000001, "timestamp": "t2"},
        ],
        "summary": {"mean": 1234.56789, "count": 2},
    }
    assert round_floats(payload) == {
        "value": 68.55,
        "values": [
            {"value": 20.3056, "timestamp": "t1"},
            {"value": 65.8, "timestamp": "t2"},
        ],
        "summary": {"mean": 1234.57, "count": 2},
    }


@pytest.mark.parametrize(
    "key",
    [
        "latitude",
        "longitude",
        "published-latitude",
        "published_longitude",
        "north",
        "south",
        "east",
        "west",
        "federal-cost",
        "non_federal_cost",
        "federal-o-and-m-cost",
    ],
)
def test_coordinate_and_money_keys_are_not_rounded(key: str) -> None:
    # A CONUS longitude would lose ~100 m at 6 sig figs; a cost would round to
    # the nearest 10k. Both pass through verbatim regardless of key casing/hyphens.
    assert round_floats({key: -118.24376543}) == {key: -118.24376543}


def test_measurement_keyed_floats_still_round() -> None:
    # The carve-out is by key name, so an ordinary measurement still rounds.
    assert round_floats({"value": -118.24376543}) == {"value": -118.244}


def test_nan_and_inf_pass_through_without_math_error() -> None:
    out = round_floats([float("nan"), float("inf"), float("-inf")])
    assert math.isnan(out[0])
    assert out[1] == float("inf")
    assert out[2] == float("-inf")


def test_zero_and_negative_zero() -> None:
    assert round_floats(0.0) == 0.0
    # -0.0 must be normalized to +0.0: `-0.0 == 0.0` is True, so an equality
    # check is vacuous — assert the sign and the serialized symptom instead.
    normalized = round_floats(-0.0)
    assert math.copysign(1.0, normalized) == 1.0
    assert json.dumps(normalized) == "0.0"


def test_negative_zero_normalized_even_for_exempt_keys() -> None:
    # The zero normalization runs ahead of the coordinate/money carve-out, so a
    # signed-zero coordinate doesn't leak "-0.0" into the JSON either.
    out = round_floats({"latitude": -0.0})
    assert math.copysign(1.0, out["latitude"]) == 1.0
    assert json.dumps(out) == '{"latitude": 0.0}'


def test_idempotent() -> None:
    once = round_floats({"value": 20.305555555555557})
    assert round_floats(once) == once


def test_custom_sig_figs() -> None:
    assert round_floats(20.305555555555557, sig_figs=2) == 20.0
    assert round_floats(20.305555555555557, sig_figs=3) == 20.3


def test_default_sig_figs_is_six() -> None:
    assert DEFAULT_SIG_FIGS == 6
