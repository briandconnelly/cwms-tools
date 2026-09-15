"""Tests for the FastMCP server: discovery resources + overview tool fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

from cwms_tools.core import overview
from cwms_tools.mcp.resources import TOOL_ERROR_CODES, capabilities_payload
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


def test_server_registers_offices_resource(server) -> None:
    async def go() -> set[str]:
        resources = await server.list_resources()
        return {str(r.uri) for r in resources}

    assert "cwms://offices" in asyncio.run(go())


def test_capabilities_advertises_offices_resource(server) -> None:
    payload = _read_json(server, "cwms://capabilities")
    uris = {r["uri"] for r in payload["resources"]}
    assert "cwms://offices" in uris


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
        assert tool.annotations.read_only_hint is True
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
    assert per_tool["cwms_get_overview_section"] == ["not_found", "usage_error"]
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
        "cwms_list_offices",
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

    result = asyncio.run(go())
    assert result.structured_content is not None


def test_overview_section_tool_carries_source_fingerprint_on_every_branch(server) -> None:
    """#70: `cwms_get_overview_section` was the one tool whose success
    responses carried no `source` at all, despite the module's own contract
    ("every successful tool response carries source.fingerprint") — fixed
    on all three success branches: index, section, and chunk."""
    from cwms_tools.core import overview

    sid = overview.section_ids()[0]

    def _branch(structured):
        return (structured or {}).get("result", structured or {})

    async def go():
        index_result = await server.call_tool("cwms_get_overview_section", arguments={})
        section_result = await server.call_tool(
            "cwms_get_overview_section", arguments={"section_id": sid, "detail": "summary"}
        )
        section_payload = _branch(section_result.structured_content)
        chunk_id = section_payload["chunks"][0]["chunk_id"]
        chunk_result = await server.call_tool(
            "cwms_get_overview_section",
            arguments={"section_id": sid, "chunk_id": chunk_id},
        )
        return index_result, section_result, chunk_result

    index_result, section_result, chunk_result = asyncio.run(go())
    assert _branch(index_result.structured_content)["source"]["fingerprint"]
    assert _branch(section_result.structured_content)["source"]["fingerprint"]
    assert _branch(chunk_result.structured_content)["source"]["fingerprint"]


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
    assert err["details"]["field"] == "section_id"
    assert err["repair"]["tool"] == "cwms_get_overview_section"
    assert err["request_id"]


def test_overview_section_resource_miss_raises_structured_jsonrpc_error(server) -> None:
    """M3/#64: a missing overview section read via the resource URI raises a
    JSON-RPC error carrying error.data in the SAME ErrorEnvelope shape the tool
    carrier uses (only the two permitted renames) — not a bespoke resource-only
    vocabulary, and no `recoverable` flag. `machine_code` is `not_found`, matching
    the code `cwms_get_overview_section` returns for the identical failure — one
    error, one code, regardless of carrier."""
    from fastmcp.exceptions import McpError

    async def go():
        return await server.read_resource("cwms://overview/does-not-exist")

    with pytest.raises(McpError) as ex:
        asyncio.run(go())
    # SEP-2164: resource-not-found is INVALID_PARAMS, same as FastMCP's own miss.
    assert ex.value.error.code == -32602
    data = ex.value.error.data
    assert isinstance(data, dict)
    assert data["machine_code"] == "not_found"
    assert data["human_message"]
    assert data["details"]["field"] == "section_id"
    assert data["details"]["value"] == "does-not-exist"
    assert data["uri"] == "cwms://overview/does-not-exist"
    assert data["repair"]["tool"] == "cwms_get_overview_section"
    assert data["temporary"] is False
    assert data["request_id"]
    assert "recoverable" not in data
    assert "code" not in data
    assert "message" not in data


def test_overview_section_resource_miss_repair_hint_is_callable(server) -> None:
    """#65 F2: the not_found repair hint must be a real, callable arg set —
    not a placeholder like `{"section_id": "<one of the listed slugs>"}` — and
    the message must enumerate the actual valid slugs (small, static set)."""
    from fastmcp.exceptions import McpError

    async def go():
        return await server.read_resource("cwms://overview/does-not-exist")

    with pytest.raises(McpError) as ex:
        asyncio.run(go())
    data = ex.value.error.data
    assert isinstance(data, dict)
    assert data["repair"]["arguments"] == {}
    for sid in overview.section_ids():
        assert sid in data["human_message"]


def test_overview_chunk_resource_miss_raises_structured_jsonrpc_error(server) -> None:
    """#64: the chunk resource's miss path uses the same envelope/carrier as the
    section miss path above — same code, same field names, no `recoverable`."""
    from fastmcp.exceptions import McpError

    sid = overview.section_ids()[0]

    async def go():
        return await server.read_resource(f"cwms://overview/{sid}/chunk/does-not-exist")

    with pytest.raises(McpError) as ex:
        asyncio.run(go())
    data = ex.value.error.data
    assert isinstance(data, dict)
    assert data["machine_code"] == "not_found"
    assert data["details"]["field"] == "chunk_id"
    assert data["details"]["value"] == "does-not-exist"
    assert data["uri"] == f"cwms://overview/{sid}/chunk/does-not-exist"
    assert data["repair"]["tool"] == "cwms_get_overview_section"
    assert data["repair"]["arguments"]["section_id"] == sid
    assert "recoverable" not in data


def test_place_tools_register_with_read_only_hint(server) -> None:
    """The four M4 place tools must register cleanly with read-only annotations."""

    async def go() -> dict[str, bool]:
        tools = {t.name: t for t in await server.list_tools()}
        return {
            name: tools[name].annotations.read_only_hint  # type: ignore[union-attr]
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


def test_overview_tool_without_section_id_returns_the_index(server) -> None:
    """#65: the only fallback tool for resource-blind clients must itself be
    discoverable without first reading the `cwms://overview` resource."""

    async def go():
        return await server.call_tool("cwms_get_overview_section", arguments={})

    result = asyncio.run(go())
    sc = result.structured_content
    assert sc is not None
    branch = sc.get("result", sc)
    assert "sections" in branch
    assert "document_sha256" in branch
    section_ids = {s["section_id"] for s in branch["sections"]}
    assert section_ids == set(overview.section_ids())
    assert "body" not in branch["sections"][0]


def test_resource_names_are_explicit_not_handler_function_names() -> None:
    """Resources/templates must register under their explicit human names, not
    the underscore-prefixed handler-function identifiers FastMCP would otherwise
    derive (e.g. `_capabilities`)."""
    import asyncio

    from cwms_tools.mcp.server import build_server

    server = build_server()
    resources = asyncio.run(server.list_resources())
    names = {r.name for r in resources}
    assert names == {"capabilities", "offices", "overview-index"}
    assert not any(n.startswith("_") for n in names)

    templates = asyncio.run(server.list_resource_templates())
    template_names = {t.name for t in templates}
    assert template_names == {"overview-section", "overview-chunk"}
    assert not any(n.startswith("_") for n in template_names)


def test_list_tools_declare_invalid_cursor():
    assert "invalid_cursor" in TOOL_ERROR_CODES["cwms_search_places"]
    assert "invalid_cursor" in TOOL_ERROR_CODES["cwms_browse_region"]


_CDA_TOOLS = {
    "cwms_search_places",
    "cwms_describe_place",
    "cwms_list_parameters",
    "cwms_get_value",
    "cwms_get_history",
    "cwms_browse_region",
    "cwms_publishers_for_parameter",
}


def test_cda_tools_declare_open_world_and_omit_idempotent_hint():
    async def go():
        mcp = build_server()
        return {t.name: t.to_mcp_tool() for t in await mcp.list_tools()}

    tools = asyncio.run(go())
    for name in _CDA_TOOLS:
        ann = tools[name].annotations
        assert ann.read_only_hint is True
        assert ann.open_world_hint is True
    overview = tools["cwms_get_overview_section"].annotations
    assert overview.open_world_hint is False


def test_every_read_only_tool_omits_idempotent_hint():
    """#75 (Copilot review): exhaustive over every registered tool, not just
    `_CDA_TOOLS` plus `cwms_get_overview_section` — that subset previously
    missed `cwms_get_profile` and `cwms_list_offices`, so a regression on
    either would have slipped through. The MCP spec only assigns
    `idempotentHint`/`destructiveHint` meaning when `readOnlyHint` is false;
    every tool here is read-only, so `idempotentHint` must be omitted
    entirely (not asserted true) rather than claiming semantics the protocol
    doesn't assign in this branch."""

    async def go():
        mcp = build_server()
        return await mcp.list_tools()

    tools = asyncio.run(go())
    assert tools  # sanity: the loop actually found and checked tools
    for tool in tools:
        ann = tool.to_mcp_tool().annotations
        assert ann.read_only_hint is True, f"{tool.name} is not read-only"
        assert ann.idempotent_hint is None, f"{tool.name} still declares idempotentHint"


def test_capabilities_declare_tool_latency():
    cap = capabilities_payload()
    lat = cap["tool_latency"]
    assert lat["cwms_get_value"] in {"network", "slow"}
    assert lat["cwms_get_history"] == "slow"
    assert lat["cwms_search_places"] == "network"
    assert lat["cwms_get_overview_section"] == "local"
    # every advertised tool has a latency class
    assert set(lat) == set(cap["tools"])


def test_capabilities_document_completion_fallback():
    comp = capabilities_payload()["completions"]
    assert comp["supported"] is False
    assert comp["discover_section_ids_via"] == "cwms://overview"


def test_list_offices_tool_is_registered_and_matches_resource(server) -> None:
    """#65 F2: office-code discovery needs a tool fallback for clients that
    cannot browse MCP resources — mirrors `cwms://offices` field-for-field."""

    async def go_tools():
        tools = await server.list_tools()
        return {t.name: t for t in tools}

    async def go_call():
        return await server.call_tool("cwms_list_offices", arguments={})

    tools = asyncio.run(go_tools())
    assert "cwms_list_offices" in tools
    tool = tools["cwms_list_offices"]
    assert tool.annotations.read_only_hint is True
    assert tool.output_schema is not None

    result = asyncio.run(go_call())
    sc = result.structured_content
    assert sc is not None
    branch = sc.get("result", sc)
    resource_payload = _read_json(server, "cwms://offices")
    assert branch == resource_payload


def test_list_offices_tool_omits_absent_fields_on_fallback_records(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#65 review: the tool must match the resource's convention of omitting
    absent optional fields rather than sending them as explicit null — the
    degraded name-only fallback path is the sharpest case (every optional
    field on `OfficeRecord` is absent)."""
    from cwms_tools.mcp import resources as resources_module

    monkeypatch.setattr(
        resources_module.offices,
        "list_offices",
        lambda **_: ([{"name": "NWDM"}], True),
    )

    async def go():
        return await server.call_tool("cwms_list_offices", arguments={})

    result = asyncio.run(go())
    branch = result.structured_content.get("result", result.structured_content)
    assert branch["offices"] == [{"name": "NWDM"}]
    assert branch["partial"] is True


def test_capabilities_advertise_list_offices_tool(server) -> None:
    payload = _read_json(server, "cwms://capabilities")
    assert "cwms_list_offices" in payload["tools"]
    assert payload["tool_error_codes"]["cwms_list_offices"] == []
    assert payload["tool_latency"]["cwms_list_offices"] == "cached"


def test_capabilities_error_handling_text_does_not_promise_defs_path(server) -> None:
    """#65 F14: deployed schemas inline all definitions — there is no literal
    `$defs/ErrorEnvelope` path in an agent's actual outputSchema, so the
    capability summary must not claim one exists."""
    payload = _read_json(server, "cwms://capabilities")
    assert "$defs" not in payload["error_handling"]["tools"]
