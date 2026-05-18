"""Capability fingerprint.

A stable SHA-256 over the inputs documented in the plan's §Discovery contract:

1. cwms-tools semver
2. cwms-python installed version
3. The sorted tool list with full input/output schemas
4. The sorted resource catalog with URI patterns
5. The error-code enum
6. The bundled cwms-overview.md SHA-256
7. The configured CDA API root

Exposed as `fingerprint` (hex) and `fingerprint_scope: "schema-contract"`.

Tool and resource registries are injected at call time so this module doesn't
need to import the FastMCP server.
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
) -> str:
    """Compute the capability fingerprint over the current server surface.

    Args:
        tools: Mapping of tool name → JSON-Schema-shaped definition (input/output
            schemas, annotations). When omitted, an empty surface is fingerprinted
            (useful for tests that just want the non-surface inputs).
        resources: List of resource records (URI pattern, mime, metadata). When
            omitted, an empty surface is fingerprinted.
    """
    payload = {
        "cwms_tools": _cwms_tools_version(),
        "cwms_python": _cwms_python_version(),
        "tools": _sorted_tools(tools or {}),
        "resources": _sorted_resources(resources or []),
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
