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
        "resources": RESOURCE_INVENTORY,
        "error_codes": sorted(c.value for c in ErrorCode),
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
        },
        "active_workarounds": active_workarounds(),
        "fastmcp": {
            "installed_version": installed_fastmcp_version(),
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
    "RESOURCE_INVENTORY",
    "SERVER_NAME",
    "SERVER_TITLE",
    "TOOL_INVENTORY",
    "capabilities_payload",
    "overview_chunk_payload",
    "overview_index_payload",
    "overview_section_payload",
]
