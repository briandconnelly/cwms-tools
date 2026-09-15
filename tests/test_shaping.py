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


# A realistic v1 Project payload (SWT/FOSS as served by CDA via cwms-python
# 1.0.9): nested location with 0.0 computed coordinates, zero costs, prose.
PROJECT_V1 = {
    "location": {"office-id": "SWT", "name": "FOSS", "latitude": 0.0, "longitude": 0.0},
    "project-owner": "BUREAU OF RECLAMATION",
    "sedimentation-desc": "verbose prose",
    "federal-cost": 0,
    "cost-unit": "$",
}


def test_place_summary_prunes_project_to_allowlist() -> None:
    out = shaping.shape_place_detail({"project": PROJECT_V1}, Detail.SUMMARY)
    # Nested location (bogus 0.0 coords), prose, and costs are dropped.
    assert out["project"] == {"project-owner": "BUREAU OF RECLAMATION"}


def test_place_full_preserves_project_verbatim() -> None:
    out = shaping.shape_place_detail({"project": PROJECT_V1}, Detail.FULL)
    assert out["project"] == PROJECT_V1


def test_place_summary_project_without_triage_fields_is_empty_not_null() -> None:
    """A project carrying only dropped fields (e.g. NWDM/FTPK: nested location +
    zero costs) shapes to `{}` — "is a project, nothing to triage" — which must
    stay distinct from `null` (not a project)."""
    only_dropped = {k: v for k, v in PROJECT_V1.items() if k not in shaping.PROJECT_SUMMARY_KEYS}
    out = shaping.shape_place_detail({"project": only_dropped}, Detail.SUMMARY)
    assert out["project"] == {}
    assert out["project"] is not None


def test_describe_project_field_description_names_summary_keys() -> None:
    """Agents learn the summary `project` contract from the MCP output schema,
    so the field description must name every key the shaper keeps."""
    from cwms_tools.core.models import DescribePlaceResponse

    desc = DescribePlaceResponse.model_fields["project"].description or ""
    for key in shaping.PROJECT_SUMMARY_KEYS:
        assert key in desc, f"project field description omits {key!r}"


def test_place_summary_leaves_null_project_null() -> None:
    out = shaping.shape_place_detail({"project": None}, Detail.SUMMARY)
    assert out["project"] is None


def test_place_summary_does_not_mutate_nested_project() -> None:
    import copy

    payload = {"project": copy.deepcopy(PROJECT_V1)}
    shaping.shape_place_detail(payload, Detail.SUMMARY)
    assert payload == {"project": PROJECT_V1}


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
