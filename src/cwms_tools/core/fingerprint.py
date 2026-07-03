"""Capability fingerprint.

A stable SHA-256 over the inputs documented in the plan's §Discovery contract:

1. cwms-tools semver
2. cwms-python installed version
3. The sorted tool list with full input/output schemas AND descriptions
4. The sorted resource/template catalog with URI patterns, names, and
   descriptions
5. The error-code enum
6. The bundled cwms-overview.md SHA-256
7. The configured CDA API root
8. The CLI command/flag/exit-code contract
9. The FastMCP runtime baseline (verified_against version, VERIFIED/FALLBACKS keys)
10. The static agent-facing capability prose (`capability_contract`: what the
    server does/does not do, error-handling and response-shape guidance,
    deprecation policy — everything in `capabilities_payload()` except values
    that are either circular (the fingerprint itself) or runtime-volatile
    (`api_root`, installed versions, already covered by inputs 1/2/9)
11. The FastMCP server `instructions` string (#71 — a prose rewrite to either
    of these that changes agent behavior must move the fingerprint, not just
    input/output schemas)

Exposed as `fingerprint` (hex) and `fingerprint_scope: "schema-contract"` — the
scope name predates #71 and is kept for backward compatibility, but the hash
has always covered more than JSON schemas (error codes, CLI contract, runtime
baseline) and now explicitly covers the agent-selection prose above too.

Tool and resource registries are injected at call time so this module doesn't
need to import the FastMCP server.  The CLI contract is likewise injected by
the caller (``mcp.contract.canonical_fingerprint``) so this module stays a
pure hash over whatever surfaces it is handed — no CLI or MCP imports here.
"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Final

from cwms_tools.core import overview
from cwms_tools.core._workarounds import active_workarounds
from cwms_tools.core.errors import ErrorCode
from cwms_tools.core.session import session_fingerprint

FINGERPRINT_SCOPE: Final[str] = "schema-contract"


def _cwms_tools_version() -> str:
    try:
        return version("cwms-tools")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


def _cwms_python_version() -> str:
    try:
        return version("cwms-python")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def compute(
    *,
    tools: dict[str, dict[str, Any]] | None = None,
    resources: list[dict[str, Any]] | None = None,
    cli_contract: dict[str, Any] | None = None,
    runtime_baseline: dict[str, Any] | None = None,
    capability_contract: dict[str, Any] | None = None,
    server_instructions: str | None = None,
) -> str:
    """Compute the capability fingerprint over the current server surface.

    Args:
        tools: Mapping of tool name → JSON-Schema-shaped definition (input/output
            schemas, description, annotations). When omitted, an empty surface is
            fingerprinted (useful for tests that just want the non-surface inputs).
        resources: List of resource records (URI pattern, name, description, mime,
            metadata). When omitted, an empty surface is fingerprinted.
        cli_contract: Structured CLI contract (commands, flags, exit codes) as
            returned by ``cli_contract_payload()``.  When omitted, an empty dict
            is used so tests that only care about other inputs are not affected.
        runtime_baseline: FastMCP capability baseline (verified_against version,
            sorted VERIFIED/FALLBACKS keys). When omitted, an empty dict is used.
            Core never imports FastMCP — the caller (mcp.contract) injects this
            as a plain dict.
        capability_contract: Static agent-facing capability prose (#71) — the
            non-circular, non-volatile subset of `capabilities_payload()`, as
            returned by `mcp.resources.capability_contract_payload()`. When
            omitted, an empty dict is used.
        server_instructions: The FastMCP server's `instructions` string (#71).
            When omitted, an empty string is used.
    """
    payload = {
        "cwms_tools": _cwms_tools_version(),
        "cwms_python": _cwms_python_version(),
        "tools": _sorted_tools(tools or {}),
        "resources": _sorted_resources(resources or []),
        "cli_contract": cli_contract or {},
        "runtime_baseline": runtime_baseline or {},
        "capability_contract": capability_contract or {},
        "server_instructions": server_instructions or "",
        "error_codes": sorted(c.value for c in ErrorCode),
        "overview_sha256": overview.document_sha256(),
        "session": session_fingerprint(),
        "workarounds": active_workarounds(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sorted_tools(tools: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": name, "definition": tools[name]} for name in sorted(tools)]


def _sorted_resources(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(resources, key=lambda r: r.get("uri", ""))


__all__ = ["FINGERPRINT_SCOPE", "compute"]
