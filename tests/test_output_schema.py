"""Guards for `mcp.output_schema` (#67).

`iserror_output_schema()` swaps FastMCP's auto-derived, per-tool-duplicated
`ErrorEnvelope` schema for one shared, hand-authored `COMPACT_ERROR_SCHEMA` —
so unlike the success branch (still derived straight from the response model,
byte-for-byte equal to what FastMCP itself would produce, per
`test_success_schema_matches_fastmcp_derivation`), the error branch can drift
from `ErrorEnvelope` silently. These tests catch that: one asserts the field
sets stay in lockstep, the other validates a real `_error_tool_result` payload
against `COMPACT_ERROR_SCHEMA` with `jsonschema`.
"""

from __future__ import annotations

import jsonschema
import pytest

from cwms_tools.core.errors import CwmsToolsError, ErrorCode, ErrorEnvelope, RepairHint, SourceInfo
from cwms_tools.core.models import ErrorRef, SearchPlacesResponse
from cwms_tools.mcp.output_schema import COMPACT_ERROR_SCHEMA, iserror_output_schema
from cwms_tools.mcp.tools import _error_tool_result, error_ref


def test_compact_error_schema_field_set_matches_error_envelope() -> None:
    """A field added to/removed from `ErrorEnvelope` must be mirrored here."""
    envelope_fields = set(ErrorEnvelope.model_fields)
    compact_fields = set(COMPACT_ERROR_SCHEMA["properties"]["error"]["properties"])
    assert compact_fields == envelope_fields


def test_compact_error_schema_required_fields_match_error_envelope() -> None:
    """A field's required/optional status changing must be mirrored here too."""
    envelope_required = set(ErrorEnvelope.model_json_schema()["required"])
    compact_required = set(COMPACT_ERROR_SCHEMA["properties"]["error"]["required"])
    assert compact_required == envelope_required


def test_compact_error_schema_code_enum_matches_error_code() -> None:
    """Resource-blind clients need the finite `code` set inline, not just via
    `cwms://capabilities` — this must include every `ErrorCode`, reserved or not."""
    schema_codes = set(COMPACT_ERROR_SCHEMA["properties"]["error"]["properties"]["code"]["enum"])
    assert schema_codes == {c.value for c in ErrorCode}


def test_compact_error_schema_repair_hint_fields_match() -> None:
    repair_fields = set(RepairHint.model_fields)
    compact_repair_fields = set(
        COMPACT_ERROR_SCHEMA["properties"]["error"]["properties"]["repair"]["properties"]
    )
    assert compact_repair_fields == repair_fields


def test_compact_error_schema_source_fields_match_source_info() -> None:
    source_fields = set(SourceInfo.model_fields)
    compact_source_fields = set(
        COMPACT_ERROR_SCHEMA["properties"]["error"]["properties"]["source"]["properties"]
    )
    assert compact_source_fields == source_fields


@pytest.mark.parametrize(
    "err",
    [
        CwmsToolsError.of(ErrorCode.NOT_FOUND, "no such place"),
        CwmsToolsError.of(
            ErrorCode.USAGE_ERROR,
            "bad limit",
            field="limit",
            offending_value=-1,
            hint="use a non-negative integer",
            repair=RepairHint(tool="cwms_search_places", args={"query": "x"}),
        ),
        CwmsToolsError.of(
            ErrorCode.RATE_LIMITED,
            "slow down",
            retryable=True,
            retry_after_ms=1000,
            endpoints_called=["/timeseries"],
        ),
    ],
)
def test_real_error_payload_validates_against_compact_schema(err: CwmsToolsError) -> None:
    """The actual JSON `_error_tool_result` emits must satisfy the advertised schema."""
    ref = error_ref(err)
    result = _error_tool_result(ref)
    assert result.structured_content is not None
    payload = result.structured_content["result"]
    jsonschema.validate(payload, COMPACT_ERROR_SCHEMA)


def test_success_schema_matches_fastmcp_derivation() -> None:
    """The success branch must stay byte-for-byte equal to FastMCP's own derivation."""
    from fastmcp.utilities.json_schema import compress_schema, dereference_refs

    built = iserror_output_schema(SearchPlacesResponse)
    expected_success = compress_schema(
        dereference_refs(SearchPlacesResponse.model_json_schema(mode="serialization")),
        prune_titles=True,
    )
    assert built["properties"]["result"]["anyOf"][0] == expected_success
    assert built["x-fastmcp-wrap-result"] is True


def test_error_ref_model_still_validates_against_compact_schema() -> None:
    """Belt-and-suspenders: `ErrorRef.model_dump()` itself satisfies the compact schema."""
    err = CwmsToolsError.of(ErrorCode.NOT_FOUND, "no such place")
    ref = ErrorRef.from_error(err)
    jsonschema.validate(ref.model_dump(mode="json"), COMPACT_ERROR_SCHEMA)
