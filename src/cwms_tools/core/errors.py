"""Unified error envelope shared across MCP tools, MCP resources, and the CLI.

The symbolic `code` field is the authoritative branch key for agents; numeric
exit codes (CLI) and JSON-RPC error data (MCP resources) derive from it. All
error codes appearing here are part of the capability fingerprint, so changes
must be intentional.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """All error codes the server can emit. Part of the capability fingerprint.

    Subclassing both `str` and `Enum` (rather than `StrEnum`, which is 3.11+) so the
    package can target Python 3.10.
    """

    GHOST_LOCATION = "ghost_location"
    GHOST_OFFICE = "ghost_office"
    PUBLISHER_UNAVAILABLE = "publisher_unavailable"
    NOT_FOUND = "not_found"
    INVALID_FIELD = "invalid_field"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    TIMEOUT = "timeout"
    WRAPPER_BUG = "wrapper_bug"
    CATALOG_CURSOR_INVALIDATED = "catalog_cursor_invalidated"
    SESSION_UNCONFIGURED = "session_unconfigured"
    TRUNCATED = "truncated"
    USAGE_ERROR = "usage_error"


# CLI exit-code map (numeric → ErrorCode). Per agent-friendly-cli §"Errors And Exit Codes".
_EXIT_CODE_MAP: dict[ErrorCode, int] = {
    ErrorCode.USAGE_ERROR: 2,
    ErrorCode.NOT_FOUND: 3,
    ErrorCode.GHOST_LOCATION: 12,
    ErrorCode.GHOST_OFFICE: 12,
    ErrorCode.PUBLISHER_UNAVAILABLE: 3,
    ErrorCode.INVALID_FIELD: 2,
    ErrorCode.RATE_LIMITED: 6,
    ErrorCode.UPSTREAM_ERROR: 9,
    ErrorCode.TIMEOUT: 7,
    ErrorCode.WRAPPER_BUG: 11,
    ErrorCode.CATALOG_CURSOR_INVALIDATED: 9,
    ErrorCode.SESSION_UNCONFIGURED: 4,
    ErrorCode.TRUNCATED: 1,
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
]
