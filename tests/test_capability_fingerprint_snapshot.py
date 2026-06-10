"""Snapshot test for the capability fingerprint.

The fingerprint covers: cwms-tools/cwms-python versions, tool list +
schemas, resource catalog, error codes, bundled overview SHA-256, session
config, active workarounds. Editing internal-only files must NOT change
the fingerprint; adding a tool, error code, or resource MUST.

This test pins the **shape** (a 64-hex SHA-256) and the **invariants**
(tools/resources/error_codes are part of the fingerprint inputs). The
actual digest is volatile across sessions because the session config
depends on the resolved User-Agent (which embeds cwms-tools version).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import fingerprint
from cwms_tools.core.errors import ErrorCode
from cwms_tools.mcp.contract import canonical_fingerprint, tool_definitions
from cwms_tools.mcp.resources import RESOURCE_INVENTORY, TOOL_INVENTORY, capabilities_payload
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


def test_dead_error_codes_removed_and_reserved_codes_declared() -> None:
    from cwms_tools.core.errors import ErrorCode
    from cwms_tools.mcp.resources import capabilities_payload

    values = {c.value for c in ErrorCode}
    assert "session_unconfigured" not in values
    assert "truncated" not in values

    payload = capabilities_payload()
    assert payload["error_codes_reserved"] == ["ghost_location", "publisher_unavailable"]
    # Reserved codes stay in the enum (they are planned contract), and the
    # live list excludes them so agents don't write dead branches.
    assert "ghost_location" not in payload["error_codes"]
    assert "publisher_unavailable" not in payload["error_codes"]
