"""Captured findings from the FastMCP 3 capability spike (M2).

The plan committed to a set of FastMCP 3 capabilities (outputSchema,
readOnlyHint, instructions, URI template query params, dual transports). This
module records which assumptions held and which need a fallback. Findings are
folded into the capability fingerprint via canonical_fingerprint's
runtime_baseline input so changes are observable.

Verified against fastmcp == 3.4.2 on 2026-06-09.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Final

#: Capabilities verified working with no fallback required.
VERIFIED: Final[dict[str, str]] = {
    "instructions_field": "FastMCP(instructions=...) is honored.",
    "tool_read_only_hint": "tool(annotations={'readOnlyHint': True}) is honored.",
    "tool_output_schema": "output_schema is auto-derived from the pydantic return type.",
    "resource_uri_templates": "Path templates with `{var}` work.",
    "resource_query_params": (
        "RFC 6570 query-form templates like `cwms://overview/{section}{?detail}` are "
        "accepted, so the plan's `?detail=summary|full` toggle is native — no need for "
        "per-detail URI fallback."
    ),
    "transport_stdio": "transport='stdio' is supported.",
    "transport_streamable_http": "transport='streamable-http' is supported.",
}

#: Capabilities that needed a documented fallback.
FALLBACKS: Final[dict[str, str]] = {
    "tool_error_iserror": (
        "raise ToolError(...) from a handler causes call_tool() to RAISE; it does not "
        "return a ToolResult with is_error=True. The ToolError exception path cannot "
        "carry structuredContent. However, returning ToolResult(is_error=True, "
        "structured_content={...}) DOES produce protocol-level isError:true alongside "
        "structuredContent (new in 3.4.x). Migration to ToolResult(is_error=True) is a "
        "breaking agent-contract change deferred to a follow-up PR; the in-band "
        "{ok:false} envelope is retained for now."
    ),
}

#: Pinned version against which VERIFIED/FALLBACKS were measured.
VERIFIED_AGAINST: Final[str] = "3.4.2"


def installed_fastmcp_version() -> str:
    try:
        return version("fastmcp")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def fastmcp_drift() -> bool:
    """Return True if the installed FastMCP differs from the spike's baseline.

    Surfaced in the capability fingerprint so consumers can re-run the spike
    after upgrades.
    """
    return installed_fastmcp_version() != VERIFIED_AGAINST


__all__ = [
    "FALLBACKS",
    "VERIFIED",
    "VERIFIED_AGAINST",
    "fastmcp_drift",
    "installed_fastmcp_version",
]
