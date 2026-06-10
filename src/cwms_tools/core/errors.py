"""Unified error envelope shared across MCP tools, MCP resources, and the CLI.

The symbolic `code` field is the authoritative branch key for agents; numeric
exit codes (CLI) and JSON-RPC error data (MCP resources) derive from it. All
error codes appearing here are part of the capability fingerprint, so changes
must be intentional.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    """All error codes the server can emit. Part of the capability fingerprint.

    Codes not yet wired to an emission path are advertised as reserved — see
    RESERVED_ERROR_CODES in cwms_tools.mcp.resources.
    """

    GHOST_LOCATION = "ghost_location"
    GHOST_OFFICE = "ghost_office"
    INVALID_CURSOR = "invalid_cursor"
    PUBLISHER_UNAVAILABLE = "publisher_unavailable"
    NOT_FOUND = "not_found"
    INVALID_FIELD = "invalid_field"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    WRAPPER_BUG = "wrapper_bug"
    USAGE_ERROR = "usage_error"


# CLI exit-code map (numeric → ErrorCode). Per agent-friendly-cli §"Errors And Exit Codes".
_EXIT_CODE_MAP: dict[ErrorCode, int] = {
    ErrorCode.USAGE_ERROR: 2,
    ErrorCode.INVALID_CURSOR: 2,
    ErrorCode.NOT_FOUND: 3,
    ErrorCode.GHOST_LOCATION: 12,
    ErrorCode.GHOST_OFFICE: 12,
    ErrorCode.PUBLISHER_UNAVAILABLE: 3,
    ErrorCode.INVALID_FIELD: 2,
    ErrorCode.RATE_LIMITED: 6,
    ErrorCode.UPSTREAM_ERROR: 9,
    ErrorCode.WRAPPER_BUG: 11,
}


def exit_code_for(code: ErrorCode) -> int:
    """Map a symbolic error code to the CLI numeric exit code."""
    return _EXIT_CODE_MAP.get(code, 1)


class RepairHint(BaseModel):
    """A pointer at a real callable surface that should succeed where this call failed."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="The MCP tool / CLI command to call next.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for the next call.",
    )


class SourceInfo(BaseModel):
    """Provenance: which endpoint(s) were called, fingerprint, any active workaround."""

    model_config = ConfigDict(extra="forbid")

    endpoints_called: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
    workaround: str | None = None


class ErrorEnvelope(BaseModel):
    """The structured error payload returned by every tool and CLI command on failure.

    Wire shape matches the plan's §"Discovery & error contracts" exactly so a single
    parser handles MCP and CLI errors.
    """

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    field: str | None = None
    offending_value: Any | None = None
    hint: str | None = None
    repair: RepairHint | None = None
    retryable: bool = False
    retry_after_ms: int | None = None
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    endpoints_called: list[str] = Field(default_factory=list)
    source: SourceInfo = Field(default_factory=SourceInfo)


def retry_after_ms_from_response(response: Any) -> int | None:
    """Parse a `Retry-After` header into milliseconds, or None.

    Accepts the two RFC 9110 forms: delta-seconds (e.g. `Retry-After: 30`) and
    an HTTP-date. Duck-types the response so this module stays decoupled from
    `requests`. A date in the past clamps to 0.
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw) * 1000
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta_ms = int((when - datetime.now(UTC)).total_seconds() * 1000)
    return max(0, delta_ms)


def upstream_error_from_status(
    status: int | None,
    *,
    endpoint: str,
    message: str,
    retry_after_ms: int | None = None,
) -> CwmsToolsError:
    """Classify an upstream HTTP failure by status code.

    - 404 → NOT_FOUND (non-retryable)
    - 429 → RATE_LIMITED (retryable; carries `retry_after_ms` when known)
    - other 4xx → UPSTREAM_ERROR (non-retryable)
    - 5xx and unknown → UPSTREAM_ERROR (retryable)

    Callers that already have an upstream exception (e.g. `cwms.api.ApiError`)
    pull `exc.response.status_code` off it and pass it in, plus
    `retry_after_ms_from_response(exc.response)` for the 429 path. Keeps this
    module decoupled from any specific upstream client.
    """
    if status == 404:
        return CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            message,
            endpoints_called=[endpoint],
            retryable=False,
        )
    if status == 429:
        return CwmsToolsError.of(
            ErrorCode.RATE_LIMITED,
            message,
            endpoints_called=[endpoint],
            retryable=True,
            retry_after_ms=retry_after_ms,
            hint=(
                "Upstream rate limit hit. Wait retry_after_ms (when set) before "
                "retrying; reduce request fan-out via CWMS_TOOLS_WORKERS."
            ),
        )
    if isinstance(status, int) and 400 <= status < 500:
        return CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            message,
            endpoints_called=[endpoint],
            retryable=False,
        )
    return CwmsToolsError.of(
        ErrorCode.UPSTREAM_ERROR,
        message,
        endpoints_called=[endpoint],
        retryable=True,
    )


class CwmsToolsError(Exception):
    """Base exception carrying an `ErrorEnvelope`. Raised by `core/*`; caught at adapters."""

    def __init__(self, envelope: ErrorEnvelope) -> None:
        super().__init__(envelope.message)
        self.envelope = envelope

    @classmethod
    def of(
        cls,
        code: ErrorCode,
        message: str,
        *,
        field: str | None = None,
        offending_value: Any | None = None,
        hint: str | None = None,
        repair: RepairHint | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        endpoints_called: list[str] | None = None,
        workaround: str | None = None,
    ) -> CwmsToolsError:
        envelope = ErrorEnvelope(
            code=code,
            message=message,
            field=field,
            offending_value=offending_value,
            hint=hint,
            repair=repair,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            endpoints_called=endpoints_called or [],
            source=SourceInfo(
                endpoints_called=endpoints_called or [],
                workaround=workaround,
            ),
        )
        return cls(envelope)


__all__ = [
    "CwmsToolsError",
    "ErrorCode",
    "ErrorEnvelope",
    "RepairHint",
    "SourceInfo",
    "exit_code_for",
    "upstream_error_from_status",
]
