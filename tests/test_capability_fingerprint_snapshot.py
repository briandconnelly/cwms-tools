"""Snapshot test for the capability fingerprint.

The fingerprint covers: cwms-tools/cwms-python versions, tool list + schemas +
descriptions, resource/template catalog + names/descriptions, error codes,
bundled overview SHA-256, session config, active workarounds, the static
capability-summary prose (`capability_contract`), and the FastMCP server
`instructions` string (#71). Editing internal-only files must NOT change the
fingerprint; adding a tool/resource/error code, or rewriting any
agent-visible description/instructions prose, MUST.

This test pins the **shape** (a 64-hex SHA-256) and the **invariants**
(tools/resources/error_codes/descriptions/instructions are part of the
fingerprint inputs). The actual digest is volatile across sessions because
the session config depends on the resolved User-Agent (which embeds
cwms-tools version).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import fingerprint
from cwms_tools.core.errors import ErrorCode
from cwms_tools.mcp import contract as contract_module
from cwms_tools.mcp import resources as resources_module
from cwms_tools.mcp.contract import (
    canonical_fingerprint,
    resource_definitions,
    server_instructions,
    tool_definitions,
)
from cwms_tools.mcp.resources import (
    RESOURCE_INVENTORY,
    TOOL_INVENTORY,
    TOOL_LATENCY,
    capabilities_payload,
    capability_contract_payload,
)
from cwms_tools.mcp.tools import _source


def test_fingerprint_shape() -> None:
    digest = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=RESOURCE_INVENTORY,
    )
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_fingerprint_changes_when_tool_added() -> None:
    base = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=RESOURCE_INVENTORY,
    )
    extended = fingerprint.compute(
        tools={
            **{name: {"name": name} for name in TOOL_INVENTORY},
            "cwms_new_hypothetical_tool": {"name": "cwms_new_hypothetical_tool"},
        },
        resources=RESOURCE_INVENTORY,
    )
    assert base != extended


def test_fingerprint_changes_when_resource_added() -> None:
    base = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=RESOURCE_INVENTORY,
    )
    extended = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=[
            *RESOURCE_INVENTORY,
            {"uri": "cwms://hypothetical", "mime_type": "application/json"},
        ],
    )
    assert base != extended


def test_fingerprint_changes_when_a_resources_error_codes_change() -> None:
    """#64: a resource's error contract (`error_codes` per `RESOURCE_INVENTORY`
    entry) is part of the fingerprint, same as `TOOL_ERROR_CODES` is for tools —
    so a breaking change like the overview resource's `section_not_found`/
    `chunk_not_found` -> `not_found` unification moves the fingerprint instead
    of leaving cached clients none the wiser."""
    base = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=RESOURCE_INVENTORY,
    )
    changed = [dict(r) for r in RESOURCE_INVENTORY]
    changed[0]["error_codes"] = [*changed[0]["error_codes"], "hypothetical_code"]
    mutated = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=changed,
    )
    assert base != mutated


def test_capabilities_cli_and_tool_source_share_canonical_fingerprint() -> None:
    """SC1: the fingerprint an agent sees must be identical across every
    surface — the `cwms://capabilities` resource, the CLI `fingerprint`
    command, and a tool response's `source.fingerprint` — so a client can
    cache by it. Previously capabilities hashed an empty tool set while the
    CLI/source hashed names only, so the three disagreed."""
    canon = canonical_fingerprint()
    cap_fp = capabilities_payload()["fingerprint"]
    source_fp = _source().fingerprint
    cli_fp = json.loads(CliRunner().invoke(app, ["fingerprint"]).stdout)["fingerprint"]
    assert canon == cap_fp == source_fp == cli_fp


def test_canonical_fingerprint_works_inside_running_event_loop() -> None:
    """`contract._run_coro` must bridge tool-schema extraction to a worker thread
    when first called from within a running loop (a live async tool handler),
    not only from plain sync code."""
    import asyncio

    from cwms_tools.mcp import contract

    contract.tool_definitions.cache_clear()  # force extraction inside the loop
    try:

        async def _main() -> str:
            return contract.canonical_fingerprint()

        digest = asyncio.run(_main())
        assert len(digest) == 64
    finally:
        contract.tool_definitions.cache_clear()


def test_fingerprint_uses_real_tool_schema_not_inventory_names() -> None:
    """SC1: the fingerprint must cover real input/output schemas (the declared
    `schema-contract` scope), not just tool names. A names-only hash would not
    move when a tool's arguments or result shape changed."""
    defs = tool_definitions()
    # Definitions carry actual schemas, not just {"name": ...}.
    sample = defs["cwms_search_places"]
    assert "properties" in (sample["input_schema"] or {})
    assert "properties" in (sample["output_schema"] or {})
    # Hashing real schemas differs from hashing names only — proves schemas count.
    names_only = fingerprint.compute(
        tools={name: {"name": name} for name in TOOL_INVENTORY},
        resources=RESOURCE_INVENTORY,
    )
    with_schemas = fingerprint.compute(tools=defs, resources=RESOURCE_INVENTORY)
    assert names_only != with_schemas


@pytest.mark.parametrize(
    "expected_tool",
    [
        "cwms_search_places",
        "cwms_describe_place",
        "cwms_list_parameters",
        "cwms_browse_region",
        "cwms_get_value",
        "cwms_get_history",
        "cwms_publishers_for_parameter",
        "cwms_get_overview_section",
    ],
)
def test_v0_1_0_tool_inventory_pins_expected_tools(expected_tool: str) -> None:
    """The v0.1.0 tool surface must contain every named tool — adding/removing
    one is a fingerprint-bumping change and forces this test to update."""
    assert expected_tool in TOOL_INVENTORY


@pytest.mark.parametrize(
    "expected_uri",
    [
        "cwms://capabilities",
        "cwms://overview",
        "cwms://overview/{section_id}{?detail}",
        "cwms://overview/{section_id}/chunk/{chunk_id}",
    ],
)
def test_v0_1_0_resource_inventory_pins_expected_uris(expected_uri: str) -> None:
    uris = {r["uri"] for r in RESOURCE_INVENTORY}
    assert expected_uri in uris


@pytest.mark.parametrize(
    "expected_code",
    [
        "ghost_location",
        "ghost_office",
        "not_found",
        "invalid_field",
        "rate_limited",
        "upstream_error",
        "wrapper_bug",
        "usage_error",
    ],
)
def test_v0_1_0_error_codes_pinned(expected_code: str) -> None:
    """Renaming or removing any of these codes is a fingerprint-bumping change."""
    values = {c.value for c in ErrorCode}
    assert expected_code in values


@pytest.mark.parametrize("dropped_code", ["timeout", "catalog_cursor_invalidated"])
def test_dropped_error_codes_not_advertised_or_in_exit_map(dropped_code: str) -> None:
    """SC2: `timeout` and `catalog_cursor_invalidated` were advertised but never
    emitted. They must not appear in the enum, the exit-code map, the capability
    summary, or the CLI schema."""
    from cwms_tools.cli.commands.schema import _schema_payload
    from cwms_tools.core.errors import _EXIT_CODE_MAP
    from cwms_tools.mcp.resources import capabilities_payload

    assert dropped_code not in {c.value for c in ErrorCode}
    assert dropped_code not in {c.value for c in _EXIT_CODE_MAP}
    assert dropped_code not in capabilities_payload()["error_codes"]
    schema = _schema_payload()
    assert dropped_code not in schema["error_codes"]
    assert dropped_code not in {row["code"] for row in schema["exit_codes"]}


def test_invalid_cursor_is_a_fingerprinted_error_code() -> None:
    assert "invalid_cursor" in {c.value for c in ErrorCode}


def test_cli_contract_is_a_fingerprint_input() -> None:
    # Two different CLI contracts must yield different digests (M-2 closed).
    base = fingerprint.compute(tools={}, resources=[], cli_contract={"commands": []})
    changed = fingerprint.compute(
        tools={}, resources=[], cli_contract={"commands": [{"path": "x"}]}
    )
    assert base != changed


def test_capabilities_declare_deprecation_policy() -> None:
    from cwms_tools.mcp.resources import capabilities_payload

    payload = capabilities_payload()
    assert payload["deprecations"] == []
    assert "remain discoverable" in payload["deprecation_policy"]


def test_dead_error_codes_removed_and_reserved_codes_declared() -> None:
    from cwms_tools.core.errors import ErrorCode
    from cwms_tools.mcp.resources import capabilities_payload

    values = {c.value for c in ErrorCode}
    assert "session_unconfigured" not in values
    assert "truncated" not in values

    payload = capabilities_payload()
    assert payload["error_codes_reserved"] == [
        "ghost_location",
        "publisher_unavailable",
        "wrapper_bug",
    ]
    # Reserved codes stay in the enum (they are planned contract), and the
    # live list excludes them so agents don't write dead branches.
    assert "ghost_location" not in payload["error_codes"]
    assert "publisher_unavailable" not in payload["error_codes"]
    assert "wrapper_bug" not in payload["error_codes"]


def test_tool_definitions_include_description_and_title() -> None:
    """#71: tool `description` is the primary agent-selection input — it must
    be live-introspected alongside the input/output schemas, not omitted."""
    defs = tool_definitions()
    sample = defs["cwms_search_places"]
    assert isinstance(sample["description"], str)
    assert len(sample["description"]) > 0
    assert sample["title"] is None or isinstance(sample["title"], str)


def test_fingerprint_changes_when_a_tool_description_changes() -> None:
    """#71: a description rewrite that changes which tool an agent picks must
    move the fingerprint, same as an input/output schema change already does."""
    base_tools = {name: {"name": name, "description": "original"} for name in TOOL_INVENTORY}
    base = fingerprint.compute(tools=base_tools, resources=RESOURCE_INVENTORY)
    changed_tools = {
        **base_tools,
        "cwms_search_places": {"name": "cwms_search_places", "description": "rewritten"},
    }
    changed = fingerprint.compute(tools=changed_tools, resources=RESOURCE_INVENTORY)
    assert base != changed


def test_resource_definitions_are_live_introspected() -> None:
    """#71: resource `name`/`title`/`description` come from the real FastMCP
    registration (server.py decorators + docstrings), not a hand-maintained
    inventory that can drift from what the server actually emits."""
    defs = resource_definitions()
    capabilities = defs["cwms://capabilities"]
    assert capabilities["name"] == "capabilities"
    assert isinstance(capabilities["description"], str)
    assert len(capabilities["description"]) > 0

    overview_section = defs["cwms://overview/{section_id}{?detail}"]
    assert overview_section["name"] == "overview-section"
    assert isinstance(overview_section["description"], str)
    # error_codes has no FastMCP introspection point — merged in from the
    # hand-maintained RESOURCE_INVENTORY, matched by URI.
    assert "not_found" in overview_section["error_codes"]


def test_resource_inventory_matches_registered_resources() -> None:
    """#71 (Codex review, round 2): the hand-maintained `RESOURCE_INVENTORY`
    (what `capabilities_payload()` advertises) and the live-introspected
    `resource_definitions()` (what the fingerprint hashes) must agree not
    just on URIs but on `mime_type` too — a hand-edited `mime_type` that
    drifts from the actual FastMCP registration would change what
    `capabilities_payload()` advertises without moving the fingerprint,
    since the fingerprint reads `mime_type` live. `error_codes` is exempt
    from this check: `resource_definitions()` sources it FROM
    `RESOURCE_INVENTORY` by construction, so it can never disagree — this
    also means a duplicate URI in `RESOURCE_INVENTORY` (which the plain
    URI-set comparison alone would hide) surfaces here as a length mismatch.
    """
    live = resource_definitions()
    assert len(RESOURCE_INVENTORY) == len(live), "RESOURCE_INVENTORY has a duplicate URI"
    assert {r["uri"] for r in RESOURCE_INVENTORY} == set(live.keys())
    for entry in RESOURCE_INVENTORY:
        assert entry["mime_type"] == live[entry["uri"]]["mime_type"], (
            f"{entry['uri']}: RESOURCE_INVENTORY mime_type has drifted from the live registration"
        )


def test_fingerprint_changes_when_a_resource_description_changes() -> None:
    """#71: same invariant as tool descriptions, for resources."""
    base_resources = [{"uri": "cwms://capabilities", "description": "original"}]
    base = fingerprint.compute(tools={}, resources=base_resources)
    changed = fingerprint.compute(
        tools={}, resources=[{"uri": "cwms://capabilities", "description": "rewritten"}]
    )
    assert base != changed


def test_fingerprint_changes_when_capability_contract_changes() -> None:
    """#71: the static capability-summary prose (what the server does/does not
    do, error-handling and response-shape guidance, deprecation policy) is a
    fingerprint input — a prose rewrite that changes agent behavior must move
    the digest even though no schema changed."""
    base = fingerprint.compute(capability_contract={"does_not": ["a"]})
    changed = fingerprint.compute(capability_contract={"does_not": ["a", "b"]})
    assert base != changed


def test_fingerprint_changes_when_server_instructions_change() -> None:
    """#71: the FastMCP server `instructions` string is client-visible
    agent-selection guidance and must move the fingerprint on rewrite."""
    base = fingerprint.compute(server_instructions="Read cwms://capabilities first.")
    changed = fingerprint.compute(server_instructions="Read cwms://offices first.")
    assert base != changed


def test_capability_contract_payload_excludes_circular_and_volatile_fields() -> None:
    """#71: `capability_contract_payload()` must stay non-circular (no
    `fingerprint`/`fingerprint_scope`) and exclude runtime-volatile values
    (`api_root`, `user_agent`, installed-version diagnostics) that would make
    the fingerprint depend on the caller's environment rather than the code."""
    contract = capability_contract_payload()
    assert "fingerprint" not in contract
    assert "fingerprint_scope" not in contract
    assert "api_root" not in contract
    assert "fastmcp" not in contract
    assert "active_workarounds" not in contract


def test_capability_contract_payload_includes_tool_latency() -> None:
    """#71 (Codex review): latency class (cached/local/network/slow) is
    agent-selection guidance same as description — a tool moving from cached
    to network changes whether an agent should call it eagerly, so it must be
    a fingerprint input too, not left out of `capability_contract_payload()`."""
    assert capability_contract_payload()["tool_latency"] == TOOL_LATENCY


def test_canonical_fingerprint_moves_with_tool_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration-level proof (not just `fingerprint.compute()` in isolation)
    that `canonical_fingerprint()` is actually wired to the new inputs."""
    base = canonical_fingerprint()
    original = dict(tool_definitions())
    mutated = {
        **original,
        "cwms_search_places": {**original["cwms_search_places"], "description": "MUTATED"},
    }
    monkeypatch.setattr(contract_module, "tool_definitions", lambda: mutated)
    assert contract_module.canonical_fingerprint() != base


def test_canonical_fingerprint_moves_with_resource_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = canonical_fingerprint()
    original = dict(resource_definitions())
    mutated = {
        **original,
        "cwms://capabilities": {**original["cwms://capabilities"], "description": "MUTATED"},
    }
    monkeypatch.setattr(contract_module, "resource_definitions", lambda: mutated)
    assert contract_module.canonical_fingerprint() != base


def test_canonical_fingerprint_moves_with_capability_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = canonical_fingerprint()
    original = capability_contract_payload()
    mutated = {**original, "does_not": [*original["does_not"], "MUTATED"]}
    monkeypatch.setattr(resources_module, "capability_contract_payload", lambda: mutated)
    assert contract_module.canonical_fingerprint() != base


def test_canonical_fingerprint_moves_with_server_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = canonical_fingerprint()
    original = server_instructions()
    monkeypatch.setattr(contract_module, "server_instructions", lambda: original + " MUTATED")
    assert contract_module.canonical_fingerprint() != base
