"""Tests for the pydantic model tiers."""

from __future__ import annotations

import json

import pytest

from cwms_tools.core.models import (
    ActiveThreshold,
    BrowseRegionResponse,
    CdaLocation,
    Detail,
    HistoryResponse,
    PlaceSummary,
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


def test_place_summary_accepts_extra_fields() -> None:
    """Task-response models allow extras so producers can add fields without
    breaking validation; the schema FastMCP derives still documents every
    declared field."""
    summary = PlaceSummary.model_validate(
        {
            "office_id": "SWT",
            "name": "FOSS",
            "parameter_count": 31,
            "additional_future_field": True,
        }
    )
    assert summary.office_id == "SWT"
    assert summary.parameter_count == 31


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
        values=[],
        value_count=0,
        source=src,
    )
    assert h.ok is True and h.next_begin is None


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
