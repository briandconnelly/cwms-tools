"""Unit tests for the error envelope and exit-code mapping."""

from __future__ import annotations

import json

import pytest

from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    ErrorEnvelope,
    RepairHint,
    exit_code_for,
)


def test_error_codes_are_stable_strings() -> None:
    """Every ErrorCode is a plain string value (so it serializes as one)."""
    for code in ErrorCode:
        assert isinstance(code.value, str)
        assert code.value == code.value.lower()


def test_exit_code_map_covers_every_error_code() -> None:
    """Every ErrorCode must map to a numeric exit (defaults to 1 for unmapped)."""
    for code in ErrorCode:
        exit_code = exit_code_for(code)
        assert 0 <= exit_code < 128


def test_ghost_office_uses_exit_12() -> None:
    assert exit_code_for(ErrorCode.GHOST_OFFICE) == 12


def test_rate_limited_uses_exit_6() -> None:
    assert exit_code_for(ErrorCode.RATE_LIMITED) == 6


def test_envelope_round_trips_through_json() -> None:
    """ErrorEnvelope must be JSON-serializable for MCP/CLI use."""
    envelope = ErrorEnvelope(
        code=ErrorCode.GHOST_OFFICE,
        message="Office NWO publishes no operational data; use the regional rollup.",
        field="office_id",
        offending_value="NWO",
        hint="Use NWDM or NWDP.",
        repair=RepairHint(
            tool="cwms_search_places",
            args={"query": "BECR", "office": "NWDM"},
        ),
    )
    blob = envelope.model_dump(mode="json")
    parsed = ErrorEnvelope.model_validate(json.loads(json.dumps(blob)))
    assert parsed.code is ErrorCode.GHOST_OFFICE
    assert parsed.repair is not None
    assert parsed.repair.tool == "cwms_search_places"
    assert parsed.repair.args["office"] == "NWDM"


def test_cwms_tools_error_of_factory_constructs_envelope() -> None:
    err = CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        "Location not found",
        field="name",
        offending_value="DOES_NOT_EXIST",
        endpoints_called=["/locations/DOES_NOT_EXIST"],
    )
    assert isinstance(err, CwmsToolsError)
    assert err.envelope.code is ErrorCode.NOT_FOUND
    assert err.envelope.source.endpoints_called == ["/locations/DOES_NOT_EXIST"]


def test_envelope_request_id_is_unique() -> None:
    a = ErrorEnvelope(code=ErrorCode.NOT_FOUND, message="x")
    b = ErrorEnvelope(code=ErrorCode.NOT_FOUND, message="x")
    assert a.request_id != b.request_id


def test_envelope_rejects_unknown_fields() -> None:
    """`extra='forbid'` on the envelope catches drift in adapters."""
    with pytest.raises(ValueError, match="Extra inputs"):
        ErrorEnvelope.model_validate({"code": "not_found", "message": "x", "unknown": "field"})
