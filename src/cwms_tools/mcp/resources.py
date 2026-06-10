"""MCP resources: capability summary, overview index, overview section bodies.

Resources here are registered on the FastMCP server in `mcp/server.py`. They
deliberately return concise summaries by default — bodies are opt-in via
`?detail=full` or the `cwms_get_overview_section` tool fallback.
"""

from __future__ import annotations

from typing import Any

from cwms_tools import __version__ as PKG_VERSION
from cwms_tools.core import fingerprint, overview
from cwms_tools.core._workarounds import active_workarounds
from cwms_tools.core.errors import ErrorCode
from cwms_tools.core.session import current_config
from cwms_tools.mcp.fastmcp_capabilities import (
    FALLBACKS,
    VERIFIED,
    VERIFIED_AGAINST,
    fastmcp_drift,
    installed_fastmcp_version,
)

SERVER_NAME = "cwms-tools"
SERVER_TITLE = "CWMS Tools — agent-friendly tools for the USACE CWMS Data API"

#: Tool name list — kept here so the capability summary stays in sync with
#: the MCP server registration without importing the server module (which
#: would create an import cycle).
TOOL_INVENTORY: list[str] = [
    "cwms_search_places",
    "cwms_describe_place",
    "cwms_list_parameters",
    "cwms_get_value",
    "cwms_get_history",
    "cwms_browse_region",
    "cwms_publishers_for_parameter",
    "cwms_get_overview_section",
]

#: Codes that exist in the enum as planned contract but have no emission path
#: yet. Advertised separately so agents don't write dead branches. Moving a
#: code from reserved to live is additive; it lands in a tool's
#: TOOL_ERROR_CODES entry when wired.
# wrapper_bug is raised in core/levels.py but absorbed into level_lookup_status
# before any agent-facing surface; it returns to the live list when that path is re-wired.
RESERVED_ERROR_CODES: list[str] = ["ghost_location", "publisher_unavailable", "wrapper_bug"]

#: Per-tool error catalog. The codes each tool can return as `error.code`, so an
#: agent can branch per tool instead of against the global enum. Curated from the
#: actual emission paths in `core/*`; part of the capability fingerprint (folded
#: into each tool's definition in `mcp/contract.py`). All CDA-hitting tools can
#: return `upstream_error`/`rate_limited`; office-scoped tools reach the NW-stub
#: guard (`ghost_office`).
TOOL_ERROR_CODES: dict[str, list[str]] = {
    "cwms_search_places": ["ghost_office", "invalid_cursor", "rate_limited", "upstream_error"],
    "cwms_describe_place": ["ghost_office", "not_found", "rate_limited", "upstream_error"],
    "cwms_list_parameters": ["ghost_office", "not_found", "rate_limited", "upstream_error"],
    "cwms_browse_region": [
        "ghost_office",
        "invalid_cursor",
        "rate_limited",
        "upstream_error",
        "usage_error",
    ],
    "cwms_get_value": ["ghost_office", "not_found", "rate_limited", "upstream_error"],
    "cwms_get_history": [
        "ghost_office",
        "invalid_field",
        "not_found",
        "rate_limited",
        "upstream_error",
    ],
    # Per-office failures are absorbed into coverage.offices_error_skipped rather
    # than failing the call, so this tool returns a result (with coverage) instead
    # of an error.code in normal operation.
    "cwms_publishers_for_parameter": [],
    "cwms_get_overview_section": ["not_found"],
}

#: Per-tool latency class (local | cached | network | slow). `slow` flags paths
#: that routinely exceed ~1s: the levels-classified value path and the
#: potentially 300k-point history pull. `cwms_get_value` is `network` for the
#: default fast path; its slow `--with-status` path is documented in the tool
#: description and `level_lookup_status`.
TOOL_LATENCY: dict[str, str] = {
    "cwms_search_places": "network",
    "cwms_describe_place": "network",
    "cwms_list_parameters": "network",
    "cwms_get_value": "network",
    "cwms_get_history": "slow",
    "cwms_browse_region": "network",
    "cwms_publishers_for_parameter": "network",
    "cwms_get_overview_section": "local",
}

#: Resource inventory — also kept here for the capability summary. Only resources
#: actually registered on `build_server()` belong here, otherwise the capability
#: summary advertises endpoints that 404 (Codex review M9 #4). `cwms://offices` and
#: `cwms://parameters` are deferred to v0.2 alongside the `cwms_publishers_for_parameter`
#: bulk index work.
RESOURCE_INVENTORY: list[dict[str, str]] = [
    {"uri": "cwms://capabilities", "mime_type": "application/json"},
    {"uri": "cwms://overview", "mime_type": "application/json"},
    {"uri": "cwms://overview/{section_id}{?detail}", "mime_type": "application/json"},
    {"uri": "cwms://overview/{section_id}/chunk/{chunk_id}", "mime_type": "application/json"},
]


def capabilities_payload() -> dict[str, Any]:
    """Build the structured capability summary served at `cwms://capabilities`.

    Per `agent-friendly-mcp` §2: states what the server does, what it does
    NOT do, prerequisites, the capability fingerprint (with scope), the tool
    and resource inventories, the FastMCP capability verdict, and the active
    workarounds. A single-read should be enough for an agent to plan against.

    The fingerprint comes from `canonical_fingerprint()` — the same value the
    CLI `fingerprint` command and every tool response's `source.fingerprint`
    report — so a client can cache by it across surfaces.
    """
    # Lazy import: contract imports this module for RESOURCE_INVENTORY.
    from cwms_tools.mcp.contract import canonical_fingerprint  # noqa: PLC0415

    cfg = current_config()
    fp = canonical_fingerprint()
    return {
        "name": SERVER_NAME,
        "title": SERVER_TITLE,
        "version": PKG_VERSION,
        "fingerprint": fp,
        "fingerprint_scope": fingerprint.FINGERPRINT_SCOPE,
        "description": (
            "Read-only tools for the USACE Corps Water Management System "
            "(CWMS) Data API. Returns task-completing answers — a current "
            "value with status context, a place description with project "
            "metadata when available, an office-scoped catalog browse — "
            "instead of mirroring the underlying REST endpoints."
        ),
        "does_not": [
            "Write, store, or delete any CWMS data.",
            "Retrieve forecasts.",
            "Serve USGS, NOAA, or any non-CWMS data sources.",
            "Decode DSS or XML forecast file attachments.",
            "Pre-warm caches or scan the catalog in the background.",
        ],
        "prerequisites": {
            "auth": "None. The CWMS Data API's read endpoints are public.",
            "api_root": cfg.api_root,
            "user_agent": cfg.user_agent,
        },
        "tools": TOOL_INVENTORY,
        "tool_error_codes": TOOL_ERROR_CODES,
        "tool_latency": TOOL_LATENCY,
        "resources": RESOURCE_INVENTORY,
        "error_codes": sorted(c.value for c in ErrorCode if c.value not in RESERVED_ERROR_CODES),
        "error_codes_reserved": list(RESERVED_ERROR_CODES),
        "error_handling": {
            "tools": (
                "Tool failures return the in-band envelope {ok: false, error: {...}} "
                "in structuredContent (FastMCP cannot set the protocol isError flag "
                "alongside structured content). Discriminate on the `ok` field, not "
                "isError. The error object carries code, message, field, hint, repair, "
                "retryable, and retry_after_ms."
            ),
            "resources": (
                "resources/read failures surface as JSON-RPC errors; the repair "
                "contract (machine_code, human_message, repair, recoverable) rides in "
                "error.data."
            ),
            "code_lists": (
                "error_codes lists codes emittable today; error_codes_reserved are "
                "planned codes with no emission path yet; tool_error_codes maps each "
                "tool to the subset it can return."
            ),
        },
        "deprecations": [],
        "deprecation_policy": (
            "Deprecated tools, resources, and error codes remain discoverable "
            "for at least one release at the same major version with an entry "
            "here naming the replacement and the removal version. Removal "
            "bumps the fingerprint. Entry shape: "
            "{name, kind, replacement, removed_in}."
        ),
        "active_workarounds": active_workarounds(),
        "completions": {
            "supported": False,
            "reason": (
                "The installed FastMCP exposes no completion handler; the overview index "
                "is the discovery path for resource-template variables."
            ),
            "discover_section_ids_via": "cwms://overview",
        },
        "fastmcp": {
            "installed_version": installed_fastmcp_version(),
            "verified_against": VERIFIED_AGAINST,
            "drift": fastmcp_drift(),
            "verified": list(VERIFIED.keys()),
            "fallbacks": list(FALLBACKS.keys()),
        },
        "discovery_hint": (
            "For the bundled CWMS orientation document, read "
            "`cwms://overview` (an index of sections) and then either "
            "`cwms://overview/{section_id}` or `cwms_get_overview_section` "
            "for a specific section. Both honor `detail=summary|full`."
        ),
    }


def overview_index_payload() -> dict[str, Any]:
    """Lightweight overview index — summaries only, no bodies."""
    sections = overview.all_sections()
    return {
        "document_sha256": overview.document_sha256(),
        "sections": [
            {
                "section_id": s.section_id,
                "title": s.title,
                "summary": s.summary,
                "size_bytes": s.size_bytes,
                "sha256": s.sha256,
                "chunk_count": s.chunk_count(),
            }
            for s in sections
        ],
    }


def overview_section_payload(
    section_id: str,
    *,
    detail: str = "summary",
) -> dict[str, Any] | None:
    """Section body keyed by stable slug.

    `detail=summary` returns the digest plus chunk metadata only.
    `detail=full` returns the body (or its first chunk if chunked).
    """
    section = overview.get_section(section_id)
    if section is None:
        return None
    chunks = section.chunks()
    payload: dict[str, Any] = {
        "section_id": section.section_id,
        "title": section.title,
        "summary": section.summary,
        "size_bytes": section.size_bytes,
        "sha256": section.sha256,
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "byte_range": list(c.byte_range),
                "sha256": c.sha256,
                "has_more": c.has_more,
            }
            for c in chunks
        ],
    }
    if detail == "full":
        # Return the body inline only if the section fits in a single chunk.
        # Multi-chunk sections force the agent to fetch chunk-by-chunk so
        # we don't ship megabytes through a single tool/resource read.
        if len(chunks) == 1:
            payload["body"] = section.body
        else:
            payload["body"] = chunks[0].text
            payload["next_chunk_id"] = chunks[1].chunk_id if len(chunks) > 1 else None
    return payload


def overview_chunk_payload(section_id: str, chunk_id: str) -> dict[str, Any] | None:
    """Fetch one chunk of a section body by stable chunk id."""
    section = overview.get_section(section_id)
    if section is None:
        return None
    chunk = section.get_chunk(chunk_id)
    if chunk is None:
        return None
    return {
        "section_id": section.section_id,
        "chunk_id": chunk.chunk_id,
        "byte_range": list(chunk.byte_range),
        "sha256": chunk.sha256,
        "has_more": chunk.has_more,
        "body": chunk.text,
    }


__all__ = [
    "RESERVED_ERROR_CODES",
    "RESOURCE_INVENTORY",
    "SERVER_NAME",
    "SERVER_TITLE",
    "TOOL_ERROR_CODES",
    "TOOL_INVENTORY",
    "TOOL_LATENCY",
    "capabilities_payload",
    "overview_chunk_payload",
    "overview_index_payload",
    "overview_section_payload",
]
