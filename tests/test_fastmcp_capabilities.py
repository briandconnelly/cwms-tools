"""Tests for the FastMCP 3 capability spike findings.

These tests pin the M2 capability spike: the assumptions documented in
`cwms_tools.mcp.fastmcp_capabilities.VERIFIED` are reachable through the
FastMCP API. Run on every PR; if FastMCP changes any of these in a
patch release, the test fails loudly so we can revisit the fingerprint
and any FALLBACKS.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from pydantic import BaseModel

from cwms_tools.mcp.fastmcp_capabilities import (
    FALLBACKS,
    VERIFIED,
    VERIFIED_AGAINST,
    fastmcp_drift,
    installed_fastmcp_version,
)


def test_verified_set_is_non_empty() -> None:
    assert "tool_read_only_hint" in VERIFIED
    assert "tool_output_schema" in VERIFIED
    assert "resource_uri_templates" in VERIFIED
    assert "resource_query_params" in VERIFIED
    assert "transport_stdio" in VERIFIED


def test_fallbacks_are_documented_when_present() -> None:
    """FALLBACKS may be empty in v0.1.0; if populated, each must have a reason."""
    for capability, reason in FALLBACKS.items():
        assert capability, "fallback key must be non-empty"
        assert reason, f"fallback for {capability} must document the reason"


def test_installed_fastmcp_version_is_a_real_version_string() -> None:
    installed = installed_fastmcp_version()
    assert installed
    assert installed != "unknown"


def test_fastmcp_drift_is_a_bool() -> None:
    """Drift signal is part of the capability fingerprint."""
    assert isinstance(fastmcp_drift(), bool)
    # Currently we are on baseline:
    assert installed_fastmcp_version() == VERIFIED_AGAINST


# --------------------------------------------------------------------------
# Live capability assertions — exercise the FastMCP API surface we depend on.
# --------------------------------------------------------------------------


class _Ping(BaseModel):
    pong: str


def _build_spike_server() -> FastMCP:
    mcp = FastMCP(name="cwms-tools-spike", instructions="spike", version="0.0.0")

    @mcp.tool(annotations={"readOnlyHint": True, "title": "Ping"})
    async def ping() -> _Ping:
        """Quickest possible probe tool."""
        return _Ping(pong="ok")

    @mcp.resource("cwms://overview/{section_id}{?detail}", mime_type="text/markdown")
    async def get_section(section_id: str, detail: str = "summary") -> str:
        return f"# {section_id} ({detail})"

    @mcp.resource("cwms://capabilities", mime_type="application/json")
    async def capabilities() -> dict[str, str]:
        return {"name": "cwms-tools"}

    return mcp


def test_tool_carries_read_only_hint_and_output_schema() -> None:
    mcp = _build_spike_server()

    async def _go() -> None:
        tools = await mcp.list_tools()
        ping = next(t for t in tools if t.name == "ping")
        assert ping.annotations is not None
        assert ping.annotations.readOnlyHint is True
        assert ping.annotations.title == "Ping"
        assert ping.output_schema is not None
        assert "pong" in ping.output_schema["properties"]

    asyncio.run(_go())


def test_resource_query_param_template_is_accepted() -> None:
    mcp = _build_spike_server()

    async def _go() -> None:
        templates = await mcp.list_resource_templates()
        uris = [t.uri_template for t in templates]
        assert any("{?detail}" in u for u in uris), uris

    asyncio.run(_go())


def test_static_and_templated_resources_coexist() -> None:
    mcp = _build_spike_server()

    async def _go() -> None:
        static = await mcp.list_resources()
        templates = await mcp.list_resource_templates()
        assert any(str(r.uri) == "cwms://capabilities" for r in static)
        assert any("cwms://overview/{section_id}" in t.uri_template for t in templates)

    asyncio.run(_go())
