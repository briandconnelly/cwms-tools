"""Guards for `mcp.output_schema` (#67).

`iserror_output_schema()` swaps FastMCP's auto-derived, per-tool-duplicated
`ErrorEnvelope` schema for one shared, hand-authored `COMPACT_ERROR_SCHEMA` —
so unlike the success branch (still derived straight from the response model,
equal to `_compact()` of what FastMCP itself would produce, per
`test_success_schema_matches_fastmcp_derivation`), the error branch can drift
from `ErrorEnvelope` silently. These tests catch that: one asserts the field
sets stay in lockstep, the other validates a real `_error_tool_result` payload
against `COMPACT_ERROR_SCHEMA` with `jsonschema`.

#181 added `_compact()`, a validation-equivalent rewrite of the assembled
schema. Its tests below pin each rule, the cases where a rule must NOT fire,
and that valid payloads pass, and invalid ones fail, both forms alike.
"""

from __future__ import annotations

import jsonschema
import pytest

from cwms_tools.core.errors import CwmsToolsError, ErrorCode, ErrorEnvelope, RepairHint, SourceInfo
from cwms_tools.core.models import (
    BrowseRegionResponse,
    DescribePlaceResponse,
    ErrorRef,
    HistoryResponse,
    ListParametersResponse,
    OfficesResponse,
    ProfileResponse,
    PublishersForParameterResponse,
    SearchPlacesResponse,
    ValueWithContextResponse,
)
from cwms_tools.mcp.output_schema import (
    COMPACT_ERROR_SCHEMA,
    _assemble_output_schema,
    _compact,
    iserror_output_schema,
)
from cwms_tools.mcp.server import OverviewIndexResponse, OverviewSectionResponse
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


def test_compact_error_schema_repair_required_fields_match() -> None:
    """#177 review: `arguments` is always sent, so both schemas require it."""
    model_required = set(RepairHint.model_json_schema()["required"])
    compact_required = set(
        COMPACT_ERROR_SCHEMA["properties"]["error"]["properties"]["repair"]["required"]
    )
    assert compact_required == model_required == {"next_step", "tool", "arguments"}


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
            value=-1,
            reason="use a non-negative integer",
            repair=RepairHint(
                next_step="retry_with_valid_limit",
                tool="cwms_search_places",
                arguments={"query": "x"},
            ),
        ),
        CwmsToolsError.of(
            ErrorCode.RATE_LIMITED,
            "slow down",
            temporary=True,
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


#: Every tool's success model(s). `test_tool_success_models_match_advertised_schemas`
#: proves this table matches what the server advertises, so other tests (and the
#: handler tests' `_call`) can rebuild each tool's uncompacted schema from it.
TOOL_SUCCESS_MODELS = {
    "cwms_get_overview_section": (OverviewSectionResponse, OverviewIndexResponse),
    "cwms_list_offices": (OfficesResponse,),
    "cwms_search_places": (SearchPlacesResponse,),
    "cwms_describe_place": (DescribePlaceResponse,),
    "cwms_list_parameters": (ListParametersResponse,),
    "cwms_browse_region": (BrowseRegionResponse,),
    "cwms_get_value": (ValueWithContextResponse,),
    "cwms_get_history": (HistoryResponse,),
    "cwms_get_profile": (ProfileResponse,),
    "cwms_publishers_for_parameter": (PublishersForParameterResponse,),
}


@pytest.mark.parametrize(
    "model",
    sorted({m for ms in TOOL_SUCCESS_MODELS.values() for m in ms}, key=lambda m: m.__name__),
)
def test_success_schema_matches_fastmcp_derivation(model) -> None:
    """The success branch must equal `_compact()` of FastMCP's own derivation."""
    from fastmcp.utilities.json_schema import compress_schema, dereference_refs

    built = iserror_output_schema(model)
    expected_success = compress_schema(
        dereference_refs(model.model_json_schema(mode="serialization")),
        prune_titles=True,
    )
    assert built["properties"]["result"]["anyOf"][0] == _compact(expected_success)
    assert _assemble_output_schema(model)["properties"]["result"]["anyOf"][0] == expected_success
    assert built["x-fastmcp-wrap-result"] is True


async def test_tool_success_models_match_advertised_schemas() -> None:
    from cwms_tools.mcp.server import build_server

    tools = {t.name: t for t in await build_server().list_tools()}
    assert set(tools) == set(TOOL_SUCCESS_MODELS)
    for name, models in TOOL_SUCCESS_MODELS.items():
        assert tools[name].output_schema == iserror_output_schema(*models), name


def test_error_ref_model_still_validates_against_compact_schema() -> None:
    """Belt-and-suspenders: `ErrorRef.model_dump()` itself satisfies the compact schema."""
    err = CwmsToolsError.of(ErrorCode.NOT_FOUND, "no such place")
    ref = ErrorRef.from_error(err)
    jsonschema.validate(ref.model_dump(mode="json"), COMPACT_ERROR_SCHEMA)


# --------------------------------------------------------------------------
# `_compact()` (#181)
# --------------------------------------------------------------------------


def test_compact_collapses_nullable_anyof_and_keeps_annotations() -> None:
    schema = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "kept",
    }
    assert _compact(schema) == {"type": ["string", "null"], "default": None, "description": "kept"}


def test_compact_dedupes_collapsed_types() -> None:
    assert _compact({"anyOf": [{"type": "string"}, {"type": "string"}]}) == {"type": "string"}


@pytest.mark.parametrize(
    "schema",
    [
        # A sibling `type` intersects the anyOf: collapsing would accept null.
        {"type": "string", "anyOf": [{"type": "string"}, {"type": "null"}]},
        # A branch with more than a bare type is not a type alternative.
        {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
        {"anyOf": [{"type": "object", "properties": {"a": {}}}, {"type": "null"}]},
        {"anyOf": [{"type": ["string", "null"]}, {"type": "integer"}]},
    ],
)
def test_compact_leaves_non_collapsible_anyof(schema) -> None:
    assert _compact(schema)["anyOf"] == schema["anyOf"]


def test_compact_keeps_null_defaults() -> None:
    """`default: null` tells an agent a missing key means null — never dropped."""
    out = _compact({"properties": {"a": {"type": "integer", "default": None}}})
    assert out["properties"]["a"]["default"] is None


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"const": False, "type": "boolean"}, {"const": False}),
        ({"const": "x", "type": "string"}, {"const": "x"}),
        ({"const": 1.0, "type": "integer"}, {"const": 1.0}),
        ({"const": 2, "type": "number"}, {"const": 2}),
        ({"enum": ["a", "b"], "type": "string"}, {"enum": ["a", "b"]}),
        ({"enum": ["a", None], "type": ["string", "null"]}, {"enum": ["a", None]}),
    ],
)
def test_compact_drops_type_implied_by_const_or_enum(schema, expected) -> None:
    assert _compact(schema) == expected


@pytest.mark.parametrize(
    "schema",
    [
        {"const": 1, "type": "string"},  # contradiction: rejects everything
        {"const": True, "type": "integer"},  # bool is not an integer in JSON Schema
        {"const": 0, "type": "boolean"},
        {"const": 1.5, "type": "integer"},
        {"enum": ["a", 1], "type": "string"},  # one member fails the type
        {"enum": [None], "type": "string"},
    ],
)
def test_compact_keeps_type_not_implied_by_const_or_enum(schema) -> None:
    assert _compact(schema)["type"] == schema["type"]


def test_compact_drops_additional_properties_true_only() -> None:
    out = _compact(
        {
            "type": "object",
            "additionalProperties": True,
            "properties": {"a": {"type": "object", "additionalProperties": False}},
        }
    )
    assert "additionalProperties" not in out
    assert out["properties"]["a"]["additionalProperties"] is False


def test_compact_keeps_additional_properties_true_beside_unevaluated_properties() -> None:
    """`additionalProperties: true` marks properties as evaluated, so dropping it
    changes what an `unevaluatedProperties` anywhere in the schema accepts."""
    schema = {
        "allOf": [{"additionalProperties": True}],
        "unevaluatedProperties": False,
    }
    assert _compact(schema) == schema
    jsonschema.validate({"x": 1}, _compact(schema))


def test_compact_never_rewrites_literal_values_or_property_names() -> None:
    literal = {"anyOf": [{"type": "string"}, {"type": "null"}], "type": "x", "const": 1}
    schema = {
        "type": "object",
        "properties": {
            # Property names that collide with schema keywords.
            "type": {"const": "a", "type": "string"},
            "default": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "anyOf": {"type": "integer"},
            "additionalProperties": {"type": "boolean"},
        },
        "default": literal,
        "examples": [literal],
        "enum": [literal],
        "const": literal,
    }
    out = _compact(schema)
    assert set(out["properties"]) == {"type", "default", "anyOf", "additionalProperties"}
    assert out["properties"]["type"] == {"const": "a"}
    assert out["properties"]["default"] == {"type": ["string", "null"]}
    assert out["properties"]["anyOf"] == {"type": "integer"}
    assert out["default"] == literal
    assert out["examples"] == [literal]
    assert out["enum"] == [literal]
    assert out["const"] == literal


def test_compact_does_not_mutate_its_input() -> None:
    import copy

    schema = _assemble_output_schema(SearchPlacesResponse)
    before = copy.deepcopy(schema)
    error_before = copy.deepcopy(COMPACT_ERROR_SCHEMA)
    _compact(schema)
    iserror_output_schema(SearchPlacesResponse)
    assert schema == before
    assert error_before == COMPACT_ERROR_SCHEMA


@pytest.mark.parametrize("name", sorted(TOOL_SUCCESS_MODELS))
def test_compacted_tool_schemas_are_valid_json_schema(name) -> None:
    schema = iserror_output_schema(*TOOL_SUCCESS_MODELS[name])
    jsonschema.Draft202012Validator.check_schema(schema)


def _both(models, payload) -> tuple[bool, bool]:
    raw = _assemble_output_schema(*models)
    compacted = iserror_output_schema(*models)
    return (
        jsonschema.Draft202012Validator(raw).is_valid(payload),
        jsonschema.Draft202012Validator(compacted).is_valid(payload),
    )


def _full_error_payload() -> dict:
    """An emitted envelope with every optional error field set."""
    err = CwmsToolsError.of(
        ErrorCode.RATE_LIMITED,
        "slow down",
        field="office",
        value="SWT",
        reason="wait, then retry",
        repair=RepairHint(
            next_step="retry_later",
            tool="cwms_search_places",
            arguments={"query": "x"},
            alternative="narrow the query",
            message="try again shortly",
        ),
        temporary=True,
        retry_after_ms=1000,
        rate_limit_remaining=0,
        endpoints_called=["/catalog/LOCATIONS"],
        workaround="none",
    )
    payload = _error_tool_result(error_ref(err)).structured_content
    assert payload is not None
    error = payload["result"]["error"]
    error["protocol_request_id"] = "42"
    assert set(error) == set(ErrorEnvelope.model_fields), "fixture must set every field"
    return payload


def _details_only_payload() -> dict:
    """A `usage_error` whose only recovery guidance is `details.reason` (places.py)."""
    err = CwmsToolsError.of(
        ErrorCode.USAGE_ERROR,
        "Pass `state` or `office`, not both.",
        field="state",
        value="CA",
        reason="`state` selects the offices to search; drop one of the two.",
    )
    payload = _error_tool_result(error_ref(err)).structured_content
    assert payload is not None
    return payload


@pytest.mark.parametrize("payload_fn", [_full_error_payload, _details_only_payload])
def test_error_payloads_pass_raw_and_compacted_schemas(payload_fn) -> None:
    for models in TOOL_SUCCESS_MODELS.values():
        assert _both(models, payload_fn()) == (True, True)


def _mutations():
    def wrong_ok(p):
        p["result"]["ok"] = 0  # `ok` is const false: 0 must still be rejected

    def null_message(p):
        p["result"]["error"]["message"] = None

    def bool_retry(p):
        p["result"]["error"]["retry_after_ms"] = True

    def float_retry(p):
        p["result"]["error"]["retry_after_ms"] = 1.5

    def unknown_code(p):
        p["result"]["error"]["code"] = "nope"

    def null_details(p):
        p["result"]["error"]["details"] = None

    def empty_details(p):
        p["result"]["error"]["details"] = {}

    def extra_error_field(p):
        p["result"]["error"]["surprise"] = 1

    def repair_missing_arguments(p):
        del p["result"]["error"]["repair"]["arguments"]

    return [
        wrong_ok,
        null_message,
        bool_retry,
        float_retry,
        unknown_code,
        null_details,
        empty_details,
        extra_error_field,
        repair_missing_arguments,
    ]


@pytest.mark.parametrize("mutate", _mutations(), ids=lambda f: f.__name__)
def test_invalid_error_payloads_fail_raw_and_compacted_schemas(mutate) -> None:
    payload = _full_error_payload()
    mutate(payload)
    assert _both(TOOL_SUCCESS_MODELS["cwms_search_places"], payload) == (False, False)


def test_integral_float_retry_after_ms_is_judged_alike() -> None:
    payload = _full_error_payload()
    payload["result"]["error"]["retry_after_ms"] = 1000.0
    raw_ok, compact_ok = _both(TOOL_SUCCESS_MODELS["cwms_search_places"], payload)
    assert raw_ok == compact_ok
