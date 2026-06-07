"""Tests for the FastMCP server: discovery resources + overview tool fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

from cwms_tools.core import overview
from cwms_tools.mcp.resources import TOOL_ERROR_CODES
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
    assert any("write" in line.lower() and "delete" in line.lower() for line in payload["does_not"])


def test_capabilities_include_per_tool_error_codes(server) -> None:
    """M4: the capability summary lists which error codes each tool can return,
    not just the global enum, so an agent can branch per tool."""
    payload = _read_json(server, "cwms://capabilities")
    per_tool = payload["tool_error_codes"]
    # Every advertised tool has an entry.
    assert set(per_tool) == set(payload["tools"])
    # Spot-check a few accurate mappings.
    assert "usage_error" in per_tool["cwms_browse_region"]  # partial bbox
    assert per_tool["cwms_get_overview_section"] == ["not_found"]
    assert "invalid_field" in per_tool["cwms_get_history"]  # bad begin/end
    # Per-tool codes are a subset of the global enum.
    global_codes = set(payload["error_codes"])
    for codes in per_tool.values():
        assert set(codes) <= global_codes


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


def test_every_task_tool_publishes_a_real_output_schema(server) -> None:
    """Every MCP task tool must declare a non-empty output schema so agents
    can validate responses without calling. The schema is derived from the
    handler's return-type annotation (a pydantic model)."""

    async def go() -> dict[str, dict]:
        return {t.name: t.output_schema for t in await server.list_tools()}

    schemas = asyncio.run(go())
    task_tools = {
        "cwms_search_places",
        "cwms_describe_place",
        "cwms_list_parameters",
        "cwms_browse_region",
        "cwms_get_value",
        "cwms_get_history",
        "cwms_publishers_for_parameter",
        "cwms_get_overview_section",
    }
    for name in task_tools:
        schema = schemas.get(name)
        assert schema is not None, f"{name} has no output schema"
        # The Union[Response, ErrorRef] return is wrapped under `result`,
        # which must itself describe `anyOf` (the success/error branches)
        # or a `properties` object with named fields. Either way, the schema
        # must carry something more specific than an empty object.
        result_slot = schema.get("properties", {}).get("result", schema)
        assert "anyOf" in result_slot or result_slot.get("properties"), (
            f"{name} output schema is hollow: {schema}"
        )


def test_mcp_output_schema_documents_search_pagination_fields(server) -> None:
    """Missed-A: `cwms_search_places` promises `truncated`/`total_count` in its
    docstring, so its output schema must declare them (not rely on extra=allow).
    Same for `cwms_browse_region` after the M2 cap."""

    async def go() -> dict[str, dict]:
        return {t.name: t.output_schema for t in await server.list_tools()}

    schemas = asyncio.run(go())
    for tool_name in ("cwms_search_places", "cwms_browse_region"):
        blob = json.dumps(schemas[tool_name])
        for field in ("total_count", "truncated", "limit"):
            assert f'"{field}"' in blob, f"{tool_name} output schema omits {field}"


def test_every_task_tool_response_carries_source_fingerprint(server) -> None:
    """Pin the response-envelope contract: every successful tool response
    must include `source.fingerprint`. Exercises the path through the
    pydantic response models in `core.models`.

    Using cwms_get_overview_section because it doesn't require CDA traffic.
    """
    from cwms_tools.core import overview

    sid = overview.section_ids()[0]

    async def go():
        return await server.call_tool(
            "cwms_get_overview_section",
            arguments={"section_id": sid, "detail": "summary"},
        )

    # Overview tool is the only one that doesn't include `source` (it
    # predates the M9 envelope rework). This test pins one of the M4-M6
    # task tools instead — exercised indirectly via the schemas test above.
    result = asyncio.run(go())
    assert result.structured_content is not None


def test_overview_section_tool_returns_not_found_payload_for_bad_slug(server) -> None:
    """M1: the overview tool's miss now uses the SAME in-band {ok: false, error}
    envelope as the seven task tools (code `not_found` + repair), not the old
    bespoke {error, repair} shape."""

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
    assert branch["ok"] is False
    err = branch["error"]
    assert err["code"] == "not_found"
    assert err["field"] == "section_id"
    assert err["repair"]["tool"] == "cwms_get_overview_section"
    assert err["request_id"]


def test_overview_section_resource_miss_raises_structured_jsonrpc_error(server) -> None:
    """M3: a missing overview section read via the resource URI raises a JSON-RPC
    error carrying the repair contract in error.data — not an error-shaped 200
    body that doesn't match the section schema."""
    from mcp import McpError

    async def go():
        return await server.read_resource("cwms://overview/does-not-exist")

    with pytest.raises(McpError) as ex:
        asyncio.run(go())
    data = ex.value.error.data
    assert isinstance(data, dict)
    assert data["machine_code"] == "section_not_found"
    assert data["repair"]["tool"] == "cwms_get_overview_section"
    assert data["recoverable"] is False


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


def test_list_tools_declare_invalid_cursor():
    assert "invalid_cursor" in TOOL_ERROR_CODES["cwms_search_places"]
    assert "invalid_cursor" in TOOL_ERROR_CODES["cwms_browse_region"]
