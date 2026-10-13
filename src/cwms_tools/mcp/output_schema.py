"""Compact `output_schema` builder for `iserror_aware` tools (#67).

Every `iserror_aware` tool's return annotation is `SomeResponse | ErrorRef` (a
Union FastMCP can't flatten into one object schema — see `mcp/tools.py` module
docstring and #64). FastMCP's default schema derivation inlines the full,
verbose `ErrorEnvelope` schema (~2.8KB) into that Union's error branch, once
per tool. Across the current tool count that repeated branch alone is over a
third of the entire `tools/list` payload every preloading client pays before
its first call (#67).

`iserror_output_schema()` reproduces exactly what FastMCP would have derived
for the success branch(es) — via the same `dereference_refs` +
`compress_schema(prune_titles=True)` pipeline FastMCP itself uses (verified
equal in `test_output_schema.py`) — and swaps in
`COMPACT_ERROR_SCHEMA`, a hand-authored but test-verified-current shape for
the error branch, in place of the auto-derived one.

The error schema is intentionally not derived from `ErrorEnvelope.model_json_schema()`
the way the success branch is: doing so would just reproduce the verbose shape
we're trying to shrink. Instead `test_output_schema.py` guards against drift by
validating real `ErrorRef` instances (as `_error_tool_result` actually emits
them) against `COMPACT_ERROR_SCHEMA` with `jsonschema`, and by asserting its
field set matches `ErrorEnvelope.model_fields` — so a field added to/removed
from `ErrorEnvelope` without a matching edit here fails loudly.

#181: the assembled schema then goes through `_compact()`, which re-expresses
it in fewer characters without changing what validates or removing anything an
agent reads (every field, constraint, description and `default` stays). The
error branch can't be shared across tools: each tool's `outputSchema` is a
standalone document, and `tools/list` has no shared-definition mechanism.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from fastmcp.utilities.json_schema import compress_schema, dereference_refs

from cwms_tools.core.errors import ErrorCode

if TYPE_CHECKING:
    from pydantic import BaseModel

#: `code`'s enum is generated from `ErrorCode` (not hand-typed) so it can't
#: drift — resource-blind clients (the `cwms_get_overview_section`/
#: `cwms_list_offices` audience) still get the finite branch-key set inline,
#: without needing to browse `cwms://capabilities` (#67 review feedback).
COMPACT_ERROR_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "description": "Failure envelope.",
    "properties": {
        "ok": {"const": False, "type": "boolean"},
        "error": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {"type": "string", "enum": [c.value for c in ErrorCode]},
                "message": {"type": "string"},
                "details": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": (
                        "Field diagnostics; omitted when none apply, never null. `reason` "
                        "may explain recovery, with or without `repair`."
                    ),
                    "minProperties": 1,
                    "properties": {
                        "field": {"type": "string"},
                        "value": {},
                        "reason": {"type": "string"},
                    },
                },
                "repair": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": "Retry: call repair.tool with repair.arguments.",
                    "properties": {
                        "next_step": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object", "additionalProperties": True},
                        "alternative": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["next_step", "tool", "arguments"],
                },
                "temporary": {"type": "boolean"},
                "retry_after_ms": {"type": "integer"},
                "rate_limit_remaining": {"type": "integer"},
                "request_id": {"type": "string"},
                "protocol_request_id": {"type": "string"},
                "source": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "endpoints_called": {"type": "array", "items": {"type": "string"}},
                        "fingerprint": {"type": "string"},
                        "workaround": {"type": "string"},
                    },
                },
            },
            "required": ["code", "message"],
        },
    },
    "required": ["error"],
}


def _success_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Reproduce FastMCP's own derived schema for one success model."""
    return compress_schema(
        dereference_refs(model.model_json_schema(mode="serialization")),
        prune_titles=True,
    )


#: Keywords whose value is a schema / a list of schemas / a map of name ->
#: schema. `_compact()` recurses only through these, so literal values under
#: `const`, `enum`, `default` or `examples`, and property *names*, are never
#: rewritten.
_SCHEMA_KEYWORDS: Final = (
    "additionalProperties",
    "unevaluatedProperties",
    "items",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedItems",
)
_SCHEMA_LIST_KEYWORDS: Final = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_MAP_KEYWORDS: Final = ("properties", "patternProperties", "$defs", "dependentSchemas")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: bool is not an integer in JSON Schema
        return "boolean"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _has_type(value: Any, declared: str | list[str]) -> bool:
    allowed = {declared} if isinstance(declared, str) else set(declared)
    actual = _json_type(value)
    return actual in allowed or (actual == "integer" and "number" in allowed)


def _compact_node(node: Any, *, drop_open_additional: bool) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_KEYWORDS:
            out[key] = _compact_node(value, drop_open_additional=drop_open_additional)
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            out[key] = [_compact_node(v, drop_open_additional=drop_open_additional) for v in value]
        elif key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {
                k: _compact_node(v, drop_open_additional=drop_open_additional)
                for k, v in value.items()
            }
        else:
            out[key] = value

    # anyOf of bare single-type branches -> a `type` list. Skipped beside a
    # sibling `type`, which would otherwise widen to the union.
    branches = out.get("anyOf")
    if (
        "type" not in out
        and isinstance(branches, list)
        and branches
        and all(
            isinstance(b, dict) and set(b) == {"type"} and isinstance(b["type"], str)
            for b in branches
        )
    ):
        types = list(dict.fromkeys(b["type"] for b in branches))
        del out["anyOf"]
        out["type"] = types[0] if len(types) == 1 else types

    # `type` beside a `const`/`enum` whose every value already has that type.
    if "type" in out and (
        ("const" in out and _has_type(out["const"], out["type"]))
        or (
            isinstance(out.get("enum"), list)
            and out["enum"]
            and all(_has_type(v, out["type"]) for v in out["enum"])
        )
    ):
        del out["type"]

    if drop_open_additional and out.get("additionalProperties") is True:
        del out["additionalProperties"]
    return out


def _compact(schema: dict[str, Any]) -> dict[str, Any]:
    """Re-express `schema` in fewer characters without changing what it accepts (#181).

    Every rule is validation-equivalent and removes nothing an agent reads:
    an `anyOf` of bare single-type branches becomes a `type` list; `type`
    beside a `const`/`enum` that already implies it is dropped; and
    `additionalProperties: true` (the default) is dropped unless the schema
    uses `unevaluatedProperties`, whose result depends on it. Annotations,
    `default: null` included, are kept. Returns a new dict and never mutates
    `schema` (so the shared `COMPACT_ERROR_SCHEMA` stays intact).
    """
    uses_unevaluated = '"unevaluatedProperties"' in json.dumps(schema)
    return _compact_node(schema, drop_open_additional=not uses_unevaluated)


def _assemble_output_schema(*success_models: type[BaseModel]) -> dict[str, Any]:
    """The uncompacted output schema: FastMCP's success derivation(s) + error branch."""
    branches = [_success_schema(m) for m in success_models]
    branches.append(COMPACT_ERROR_SCHEMA)
    return {
        "type": "object",
        "properties": {"result": {"anyOf": branches}},
        "required": ["result"],
        "x-fastmcp-wrap-result": True,
    }


def iserror_output_schema(*success_models: type[BaseModel]) -> dict[str, Any]:
    """Build the `output_schema` override for an `iserror_aware` tool.

    Pass every success-branch model the tool can return (usually one; two for
    `cwms_get_overview_section`, which can return the index or a section). The
    error branch is always `COMPACT_ERROR_SCHEMA`; the whole schema then goes
    through `_compact()` (#181).
    """
    return _compact(_assemble_output_schema(*success_models))


__all__ = ["COMPACT_ERROR_SCHEMA", "iserror_output_schema"]
