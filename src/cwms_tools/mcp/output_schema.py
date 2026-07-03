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
byte-for-byte equal in `test_output_schema.py`) — and swaps in
`COMPACT_ERROR_SCHEMA`, a hand-authored but test-verified-current shape for
the error branch, in place of the auto-derived one.

The error schema is intentionally not derived from `ErrorEnvelope.model_json_schema()`
the way the success branch is: doing so would just reproduce the verbose shape
we're trying to shrink. Instead `test_output_schema.py` guards against drift by
validating real `ErrorRef` instances (as `_error_tool_result` actually emits
them) against `COMPACT_ERROR_SCHEMA` with `jsonschema`, and by asserting its
field set matches `ErrorEnvelope.model_fields` — so a field added to/removed
from `ErrorEnvelope` without a matching edit here fails loudly.
"""

from __future__ import annotations

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
                "field": {"type": ["string", "null"]},
                "offending_value": {},
                "hint": {"type": ["string", "null"]},
                "repair": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "description": "Retry: call repair.tool with repair.args.",
                    "properties": {
                        "tool": {"type": "string"},
                        "args": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["tool"],
                },
                "retryable": {"type": "boolean"},
                "retry_after_ms": {"type": ["integer", "null"]},
                "request_id": {"type": "string"},
                "protocol_request_id": {"type": ["string", "null"]},
                "endpoints_called": {"type": "array", "items": {"type": "string"}},
                "source": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "endpoints_called": {"type": "array", "items": {"type": "string"}},
                        "fingerprint": {"type": ["string", "null"]},
                        "workaround": {"type": ["string", "null"]},
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


def iserror_output_schema(*success_models: type[BaseModel]) -> dict[str, Any]:
    """Build the `output_schema` override for an `iserror_aware` tool.

    Pass every success-branch model the tool can return (usually one; two for
    `cwms_get_overview_section`, which can return the index or a section). The
    error branch is always `COMPACT_ERROR_SCHEMA`.
    """
    branches = [_success_schema(m) for m in success_models]
    branches.append(COMPACT_ERROR_SCHEMA)
    return {
        "type": "object",
        "properties": {"result": {"anyOf": branches}},
        "required": ["result"],
        "x-fastmcp-wrap-result": True,
    }


__all__ = ["COMPACT_ERROR_SCHEMA", "iserror_output_schema"]
