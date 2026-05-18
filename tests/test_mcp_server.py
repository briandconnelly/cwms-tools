"""Tests for the FastMCP server: discovery resources + overview tool fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

from cwms_tools.core import overview
from cwms_tools.mcp.server import build_server


@pytest.fixture
def server():
    return build_server()


def _read_json(server, uri: str) -> dict:
    """Helper: read a JSON-typed resource and parse its body."""

    async def go() -> dict:
        result = await server.read_resource(uri)
        # ResourceResult.contents is a list of ResourceContent objects with a
        # `.content` attribute holding the body as a string.
        for item in result.contents:
            payload = getattr(item, "content", None) or getattr(item, "text", None)
            if payload:
                return json.loads(payload)
        raise AssertionError(f"no JSON content for {uri}")

    return asyncio.run(go())


def test_server_registers_capabilities_and_overview_index(server) -> None:
    async def go() -> set[str]:
        resources = await server.list_resources()
        return {str(r.uri) for r in resources}

    uris = asyncio.run(go())
    assert "cwms://capabilities" in uris
    assert "cwms://overview" in uris


def test_server_registers_overview_section_and_chunk_templates(server) -> None:
    async def go() -> list[str]:
        templates = await server.list_resource_templates()
        return [t.uri_template for t in templates]

    templates = asyncio.run(go())
    assert any("cwms://overview/{section_id}{?detail}" in t for t in templates)
    assert any("cwms://overview/{section_id}/chunk/{chunk_id}" in t for t in templates)


def test_overview_section_tool_is_registered_as_read_only(server) -> None:
    async def go() -> None:
        tools = await server.list_tools()
        names = [t.name for t in tools]
        assert "cwms_get_overview_section" in names
        tool = next(t for t in tools if t.name == "cwms_get_overview_section")
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.output_schema is not None

    asyncio.run(go())


def test_capabilities_resource_reads_back_with_fingerprint(server) -> None:
    payload = _read_json(server, "cwms://capabilities")
    assert payload["name"] == "cwms-tools"
    assert payload["fingerprint_scope"] == "schema-contract"
    assert "cwms_get_overview_section" in payload["tools"]
    assert any(c == "ghost_office" for c in payload["error_codes"])
    assert any("read-only in v0.1.0" in line for line in payload["does_not"])


def test_overview_index_returns_summary_only(server) -> None:
    payload = _read_json(server, "cwms://overview")
    assert "sections" in payload
    section_ids = {s["section_id"] for s in payload["sections"]}
    assert section_ids == set(overview.section_ids())
    # No bodies inlined.
    assert all("body" not in s for s in payload["sections"])


def test_overview_section_resource_supports_summary_and_full(server) -> None:
    sid = overview.section_ids()[0]
    summary = _read_json(server, f"cwms://overview/{sid}")
    full = _read_json(server, f"cwms://overview/{sid}?detail=full")
    assert summary["section_id"] == sid
    assert "body" not in summary
    assert "body" in full


def test_overview_section_tool_returns_not_found_payload_for_bad_slug(server) -> None:
    async def go():
        return await server.call_tool(
            "cwms_get_overview_section",
            arguments={"section_id": "does-not-exist"},
        )

    result = asyncio.run(go())
    # FastMCP wraps a Union return in {"result": <chosen branch>}.
    sc = result.structured_content
    assert sc is not None
    branch = sc.get("result", sc)  # tolerate both shapes
    assert branch.get("error") == "section_not_found"
    assert branch["repair"]["tool"] == "cwms_get_overview_section"


def test_place_tools_register_with_read_only_hint(server) -> None:
    """The four M4 place tools must register cleanly with read-only annotations."""

    async def go() -> dict[str, bool]:
        tools = {t.name: t for t in await server.list_tools()}
        return {
            name: tools[name].annotations.readOnlyHint  # type: ignore[union-attr]
            for name in (
                "cwms_search_places",
                "cwms_describe_place",
                "cwms_list_parameters",
                "cwms_browse_region",
            )
            if name in tools
        }

    found = asyncio.run(go())
    assert set(found.keys()) == {
        "cwms_search_places",
        "cwms_describe_place",
        "cwms_list_parameters",
        "cwms_browse_region",
    }
    assert all(found.values())


def test_overview_section_tool_returns_section_for_good_slug(server) -> None:
    sid = overview.section_ids()[0]

    async def go():
        return await server.call_tool(
            "cwms_get_overview_section",
            arguments={"section_id": sid, "detail": "full"},
        )

    result = asyncio.run(go())
    sc = result.structured_content
    assert sc is not None
    branch = sc.get("result", sc)
    assert branch["section_id"] == sid
    assert "body" in branch
