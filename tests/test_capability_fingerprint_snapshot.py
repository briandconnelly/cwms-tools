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

import pytest

from cwms_tools.core import fingerprint
from cwms_tools.core.errors import ErrorCode
from cwms_tools.mcp.resources import RESOURCE_INVENTORY, TOOL_INVENTORY


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
        "cwms://offices",
        "cwms://parameters",
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
        "timeout",
        "wrapper_bug",
        "usage_error",
        "catalog_cursor_invalidated",
    ],
)
def test_v0_1_0_error_codes_pinned(expected_code: str) -> None:
    """Renaming or removing any of these codes is a fingerprint-bumping change."""
    values = {c.value for c in ErrorCode}
    assert expected_code in values
