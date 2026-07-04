"""Tests for the pydantic model tiers."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from cwms_tools.core.models import (
    ActiveThreshold,
    BrowseRegionResponse,
    CdaLocation,
    Detail,
    HistoryResponse,
    LevelLookupStatus,
    PlaceSummary,
    Rollup,
    SearchPlacesResponse,
    SourceMeta,
    StatusClass,
    TsIdParts,
    ValueWithContextResponse,
)


def test_cda_location_accepts_hyphenated_aliases_and_extras() -> None:
    raw = {
        "name": "FTPK",
        "office-id": "NWDM",
        "location-kind": "PROJECT",
        "horizontal-datum": "NAD83",
        "latitude": 47.991,
        "longitude": -106.412,
        "some-future-field": {"foo": "bar"},  # extra="allow" tolerates this
    }
    loc = CdaLocation.model_validate(raw)
    assert loc.office_id == "NWDM"
    assert loc.location_kind == "PROJECT"
    dumped = loc.model_dump(by_alias=True)
    assert "some-future-field" in dumped


def test_ts_id_parts_round_trip() -> None:
    parts = TsIdParts(
        location="FOSS",
        parameter="Elev",
        type="Inst",
        interval="15Minutes",
        duration="0",
        version="Ccp-Rev",
    )
    assert parts.ts_id == "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"


def test_detail_enum_string_values() -> None:
    assert Detail.SUMMARY.value == "summary"
    assert Detail.FULL.value == "full"


def test_status_class_enum_values() -> None:
    expected = {"nominal", "watch", "action", "flood", "unknown"}
    assert {c.value for c in StatusClass} == expected


def test_place_summary_rejects_extra_fields() -> None:
    """#74: task-response models are closed (extra='forbid') so an undeclared
    producer field surfaces as a loud ValidationError instead of silently
    passing through undocumented and unfingerprinted."""
    with pytest.raises(ValidationError):
        PlaceSummary.model_validate(
            {
                "office_id": "SWT",
                "name": "FOSS",
                "parameter_count": 31,
                "additional_future_field": True,
            }
        )


def test_search_places_response_serializes_to_json() -> None:
    result = SearchPlacesResponse(
        query="Fort Peck",
        office="NWDM",
        results=[
            PlaceSummary(
                office_id="NWDM",
                name="FTPK",
                public_name="Fort Peck Lake",
                location_kind="PROJECT",
                latitude=47.991,
                longitude=-106.412,
                parameter_count=10,
                publishers=["Best-MRBWM", "Raw-A2W"],
                last_data_timestamp="2026-05-17T18:00:00Z",
                co_located=["FTPK1"],
            )
        ],
        source=SourceMeta(fingerprint="abc123"),
    )
    blob = result.model_dump(mode="json")
    parsed = SearchPlacesResponse.model_validate(json.loads(json.dumps(blob)))
    assert parsed.results[0].publishers == ["Best-MRBWM", "Raw-A2W"]
    assert parsed.source.fingerprint == "abc123"


def test_active_threshold_relation_is_literal_enum() -> None:
    t = ActiveThreshold(
        specified_level_id="Flood Stage",
        value=15.0,
        unit="ft",
        relation="above",
        delta=0.5,
    )
    assert t.relation == "above"


def test_active_threshold_carries_level_id_and_source_workaround() -> None:
    """#74: these are real fields (present at detail=full, stripped in
    summary by `shape_value_detail`), not extras tolerated by the model."""
    t = ActiveThreshold(
        specified_level_id="Flood Stage",
        level_id="Carlyle Lk.Elev.Inst.0.Flood Stage",
        value=15.0,
        unit="ft",
        relation="above",
        source_workaround="rating-curve-fallback",
    )
    assert t.level_id == "Carlyle Lk.Elev.Inst.0.Flood Stage"
    assert t.source_workaround == "rating-curve-fallback"
    # Both are optional (absent in summary-mode payloads).
    bare = ActiveThreshold(
        specified_level_id="Flood Stage", value=15.0, unit="ft", relation="above"
    )
    assert bare.level_id is None
    assert bare.source_workaround is None


def test_publishers_for_parameter_response_round_trips_observed_publishers_alias() -> None:
    """#74: `_observed_publishers_by_office` is a real (aliased) field now,
    not an extra — pydantic forbids a literal underscore-prefixed field name,
    so the wire key is preserved via `alias` + `serialize_by_alias=True`."""
    from cwms_tools.core.models import PublishersCoverage, PublishersForParameterResponse

    resp = PublishersForParameterResponse(
        parameter="Elev",
        publishers=[],
        publisher_count=0,
        ts_count=0,
        coverage=PublishersCoverage(
            offices_requested=["NWDM"],
            offices_indexed=["NWDM"],
            complete=True,
        ),
        source=SourceMeta(fingerprint="f" * 64),
        _observed_publishers_by_office={"NWDM": ["Best-MRBWM"]},
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["_observed_publishers_by_office"] == {"NWDM": ["Best-MRBWM"]}
    assert "observed_publishers_by_office" not in dumped


@pytest.mark.parametrize(
    "model_cls",
    [
        PlaceSummary,
        SearchPlacesResponse,
        BrowseRegionResponse,
        ActiveThreshold,
        ValueWithContextResponse,
        HistoryResponse,
    ],
)
def test_task_response_models_are_closed(model_cls: type[BaseModel]) -> None:
    """#74: task-response models forbid extras — an undeclared producer field
    is a schema bug to fix, not a silently-tolerated passthrough. (The DTO
    facade tier — CdaLocation/CdaProject — is a different tier and is exempt
    by design; not parametrized here.)"""
    assert model_cls.model_config.get("extra") == "forbid"


def test_value_with_context_response_carries_source_meta() -> None:
    summary = ValueWithContextResponse(
        ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        office_id="SWT",
        location="FOSS",
        parameter="Elev",
        publisher="Ccp-Rev",
        value=1648.21,
        unit="ft",
        timestamp="2026-05-17T18:00:00Z",
        status_class=StatusClass.NOMINAL,
        thresholds_active=[],
        level_lookup_status=LevelLookupStatus.SKIPPED,
        source=SourceMeta(fingerprint="abc"),
    )
    assert summary.source.fingerprint == "abc"


def test_active_threshold_rejects_bad_relation() -> None:
    with pytest.raises(ValueError, match=r"(?i)relation|input should be"):
        ActiveThreshold.model_validate(
            {
                "specified_level_id": "Flood",
                "value": 1.0,
                "unit": "ft",
                "relation": "near",  # not in Literal
            }
        )


def test_success_models_default_ok_true_and_carry_cursor_fields():
    src = SourceMeta(fingerprint="abc")
    s = SearchPlacesResponse(query="x", results=[], source=src)
    assert s.ok is True
    assert s.has_more is False
    assert s.next_cursor is None

    b = BrowseRegionResponse(office="SWT", bbox=None, state=None, results=[], source=src)
    assert b.ok is True and b.has_more is False and b.next_cursor is None

    h = HistoryResponse(
        ts_id="t",
        office_id="o",
        location="l",
        parameter="p",
        publisher=None,
        unit="EN",
        begin="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
        rollup=Rollup.RAW,
        summary=None,
        values=[],
        value_count=0,
        source=src,
    )
    assert h.ok is True and h.next_begin is None
    # rollup/summary/values are required (no defaults masking missing keys).
    with pytest.raises(ValueError, match=r"(?i)rollup|summary|field required"):
        HistoryResponse.model_validate(
            {
                "ts_id": "t",
                "office_id": "o",
                "location": "l",
                "parameter": "p",
                "unit": "EN",
                "begin": "2026-01-01T00:00:00Z",
                "end": "2026-01-02T00:00:00Z",
                "value_count": 0,
                "source": src.model_dump(mode="json"),
            }
        )


def test_error_ref_error_field_is_typed_envelope() -> None:
    """The outputSchema must expose the error envelope's fields, not an opaque object."""
    from cwms_tools.core.errors import ErrorEnvelope
    from cwms_tools.core.models import ErrorRef

    schema = ErrorRef.model_json_schema()
    # The error property must reference the envelope definition, not be a bare object.
    error_prop = schema["properties"]["error"]
    assert error_prop == {"$ref": "#/$defs/ErrorEnvelope"}

    ref = ErrorRef.model_validate({"ok": False, "error": {"code": "not_found", "message": "nope"}})
    assert isinstance(ref.error, ErrorEnvelope)
    assert ref.error.code.value == "not_found"


def test_error_ref_from_error_copies_the_envelope() -> None:
    """from_error deep-copies so mutating ref.error never aliases the exception."""
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode
    from cwms_tools.core.models import ErrorRef

    err = CwmsToolsError.of(ErrorCode.NOT_FOUND, "nope")
    ref = ErrorRef.from_error(err)
    assert ref.error is not err.envelope
    ref.error.source.fingerprint = "f" * 64
    assert err.envelope.source.fingerprint is None


def test_search_response_drops_null_fields_on_dump() -> None:
    from cwms_tools.core.models import SearchPlacesResponse, SourceMeta

    resp = SearchPlacesResponse(
        query="x",
        results=[],
        source=SourceMeta(fingerprint="f" * 64),
    )
    dumped = resp.model_dump(mode="json")
    assert "parameter" not in dumped
    assert "nearby_non_matching_count" not in dumped
    assert "next_cursor" not in dumped
    assert dumped["query"] == "x"  # non-null fields stay


def test_value_response_keeps_semantic_nulls() -> None:
    from cwms_tools.core.models import (
        LevelLookupStatus,
        SourceMeta,
        StatusClass,
        ValueWithContextResponse,
    )

    resp = ValueWithContextResponse(
        ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        office_id="SWT",
        location="FOSS",
        parameter="Elev",
        publisher=None,
        value=None,
        unit="ft",
        timestamp=None,
        status_class=StatusClass.UNKNOWN,
        thresholds_active=[],
        level_lookup_status=LevelLookupStatus.SKIPPED,
        source=SourceMeta(fingerprint="f" * 64),
    )
    dumped = resp.model_dump(mode="json")
    assert "value" in dumped and dumped["value"] is None  # null means "no observation"
    assert "timestamp" in dumped and dumped["timestamp"] is None
    assert "publisher" not in dumped  # non-semantic null is stripped


def test_serialization_mode_schema_keeps_fields() -> None:
    """Guard the __get_pydantic_json_schema__ override in core/_compact.py: a
    pydantic upgrade that bypasses it collapses every tool outputSchema to
    {additionalProperties: true} with no properties."""
    from pydantic import TypeAdapter

    from cwms_tools.core.models import SearchPlacesResponse

    schema = TypeAdapter(SearchPlacesResponse).json_schema(mode="serialization")
    assert "query" in schema["properties"]
    assert "results" in schema["properties"]


def test_value_response_rounds_conversion_noise_on_dump() -> None:
    """Issue #45: the model serializer rounds measurement floats to 6 sig figs."""
    from cwms_tools.core.models import (
        LevelLookupStatus,
        SourceMeta,
        StatusClass,
        ValueWithContextResponse,
    )

    resp = ValueWithContextResponse(
        ts_id="BBLW_S1-D1,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV",
        office_id="NWDP",
        location="BBLW_S1-D1,0ft",
        parameter="Temp-Water",
        value=68.55000000000001,
        unit="degF",
        timestamp="2026-05-17T18:00:00Z",
        status_class=StatusClass.UNKNOWN,
        thresholds_active=[],
        level_lookup_status=LevelLookupStatus.SKIPPED,
        source=SourceMeta(fingerprint="f" * 64),
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["value"] == 68.55


def test_place_summary_preserves_coordinates_but_rounds_other_floats() -> None:
    """Lat/lon carve-out survives serialization; a declared nested float
    (parsed sensor depth) rounds normally. Generic float-rounding itself is
    covered exhaustively in tests/test_rounding.py; this just checks a real
    PlaceSummary field, not a fabricated extra="allow" one (#74)."""
    summary = PlaceSummary.model_validate(
        {
            "office_id": "NWDM",
            "name": "FTPK",
            "latitude": 47.99123456,
            "longitude": -106.41234567,
            "depth": {"value": 20.305555555555557, "unit": "ft"},
        }
    )
    dumped = summary.model_dump(mode="json")
    assert dumped["latitude"] == 47.99123456  # citation-grade precision preserved
    assert dumped["longitude"] == -106.41234567
    assert dumped["depth"]["value"] == 20.3056


def test_compact_models_round_trip() -> None:
    from cwms_tools.core.models import DescribePlaceResponse, SourceMeta

    resp = DescribePlaceResponse(
        office_id="SWT",
        name="FOSS",
        location={},
        project=None,
        partial=False,
        partial_reasons=[],
        parameters=[],
        parameter_count=0,
        publishers=[],
        ts_ids=[],
        last_data_timestamp=None,
        source=SourceMeta(fingerprint="f" * 64),
    )
    assert DescribePlaceResponse.model_validate(resp.model_dump(mode="json"))
