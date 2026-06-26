"""Unit tests for the shared `core.shaping` detail-shaping functions (#56).

These pin the pruning each shaper performs and that all are copy-on-write
(the producer payload is never mutated in place).
"""

from __future__ import annotations

from cwms_tools.core import shaping
from cwms_tools.core.models import Detail


def test_place_summary_prunes_location_and_results() -> None:
    payload = {
        "location": {"office-id": "SWT", "name": "FOSS", "description": "verbose"},
        "results": [{"name": "FOSS", "raw": {"big": "blob"}}],
    }
    out = shaping.shape_place_detail(payload, Detail.SUMMARY)
    assert "description" not in out["location"]  # not in LOCATION_SUMMARY_KEYS
    assert out["location"] == {"office-id": "SWT", "name": "FOSS"}
    assert out["results"] == [{"name": "FOSS"}]  # raw stripped


def test_place_full_preserves_everything() -> None:
    payload = {
        "location": {"office-id": "SWT", "name": "FOSS", "description": "verbose"},
        "results": [{"name": "FOSS", "raw": {"big": "blob"}}],
    }
    out = shaping.shape_place_detail(payload, Detail.FULL)
    assert out == payload


def test_value_summary_strips_threshold_internals() -> None:
    payload = {
        "thresholds_active": [
            {"specified_level_id": "Top", "value": 1.0, "level_id": "X", "source_workaround": "w"}
        ]
    }
    out = shaping.shape_value_detail(payload, Detail.SUMMARY)
    assert out["thresholds_active"] == [{"specified_level_id": "Top", "value": 1.0}]
    assert shaping.shape_value_detail(payload, Detail.FULL) == payload


def test_history_summary_strips_quality() -> None:
    payload = {"values": [{"timestamp": "t", "value": 1.0, "quality": 3}]}
    out = shaping.shape_history_detail(payload, Detail.SUMMARY)
    assert out["values"] == [{"timestamp": "t", "value": 1.0}]
    assert shaping.shape_history_detail(payload, Detail.FULL) == payload


def test_profile_summary_strips_ts_id() -> None:
    payload = {"profile": [{"name": "S1", "depth": 3.0, "ts_id": "TSID"}]}
    out = shaping.shape_profile_detail(payload, Detail.SUMMARY)
    assert out["profile"] == [{"name": "S1", "depth": 3.0}]
    assert shaping.shape_profile_detail(payload, Detail.FULL) == payload


def test_publishers_summary_pops_internal_field() -> None:
    payload = {"parameter": "Elev", "_observed_publishers_by_office": {"NWDM": ["Best-MRBWM"]}}
    out = shaping.shape_publishers_detail(payload, Detail.SUMMARY)
    assert "_observed_publishers_by_office" not in out
    assert shaping.shape_publishers_detail(payload, Detail.FULL) == payload


def test_shapers_do_not_mutate_input() -> None:
    """Copy-on-write: the producer payload must be untouched after shaping."""
    cases = [
        (shaping.shape_place_detail, {"results": [{"name": "F", "raw": {"x": 1}}]}),
        (shaping.shape_value_detail, {"thresholds_active": [{"value": 1.0, "level_id": "X"}]}),
        (shaping.shape_history_detail, {"values": [{"value": 1.0, "quality": 3}]}),
        (shaping.shape_profile_detail, {"profile": [{"name": "S", "ts_id": "T"}]}),
        (shaping.shape_publishers_detail, {"_observed_publishers_by_office": {"a": ["b"]}}),
    ]
    for fn, payload in cases:
        import copy

        snapshot = copy.deepcopy(payload)
        fn(payload, Detail.SUMMARY)
        assert payload == snapshot, f"{fn.__name__} mutated its input"
