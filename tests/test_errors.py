"""Unit tests for the error envelope and exit-code mapping."""

from __future__ import annotations

import json
from email.utils import format_datetime, parsedate_to_datetime

import pytest

from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    ErrorEnvelope,
    RepairHint,
    exit_code_for,
    retry_after_ms_from_response,
    upstream_error_from_status,
)


class _FakeResponse:
    """Minimal duck-typed response carrying just headers (for Retry-After tests)."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


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
            tool="cwms_browse_region",
            args={"office": "NWDM"},
        ),
    )
    blob = envelope.model_dump(mode="json")
    parsed = ErrorEnvelope.model_validate(json.loads(json.dumps(blob)))
    assert parsed.code is ErrorCode.GHOST_OFFICE
    assert parsed.repair is not None
    assert parsed.repair.tool == "cwms_browse_region"
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


def test_upstream_429_maps_to_rate_limited_retryable_with_retry_after_ms() -> None:
    """SC2: a 429 is the rate-limit repair signal — retryable, with the wait
    encoded in `retry_after_ms`, not the previous non-retryable upstream_error."""
    err = upstream_error_from_status(
        429,
        endpoint="/catalog/LOCATIONS",
        message="Upstream returned 429 for /catalog/LOCATIONS.",
        retry_after_ms=30_000,
    )
    env = err.envelope
    assert env.code is ErrorCode.RATE_LIMITED
    assert env.retryable is True
    assert env.retry_after_ms == 30_000
    assert exit_code_for(env.code) == 6


def test_non_404_4xx_remains_non_retryable_upstream_error() -> None:
    """Regression guard: a 403 stays a non-retryable upstream_error (only 429
    flips to rate_limited)."""
    err = upstream_error_from_status(403, endpoint="/x", message="forbidden")
    assert err.envelope.code is ErrorCode.UPSTREAM_ERROR
    assert err.envelope.retryable is False


def test_retry_after_ms_from_response_parses_delta_seconds() -> None:
    assert retry_after_ms_from_response(_FakeResponse({"Retry-After": "30"})) == 30_000


def test_retry_after_ms_from_response_parses_http_date() -> None:
    # An HTTP-date a minute in the future resolves to roughly 60_000 ms.
    future = parsedate_to_datetime("Wed, 21 Oct 2099 07:28:00 GMT")
    val = retry_after_ms_from_response(_FakeResponse({"Retry-After": format_datetime(future)}))
    assert val is not None and val > 0


def test_retry_after_ms_from_response_handles_missing_or_bad_values() -> None:
    assert retry_after_ms_from_response(_FakeResponse({})) is None
    assert retry_after_ms_from_response(None) is None
    assert retry_after_ms_from_response(_FakeResponse({"Retry-After": "garbage"})) is None


def test_invalid_cursor_code_maps_to_usage_exit() -> None:
    assert ErrorCode.INVALID_CURSOR.value == "invalid_cursor"
    assert exit_code_for(ErrorCode.INVALID_CURSOR) == 2
