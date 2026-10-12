"""`issues_url` and defect-report guidance on every agent-facing surface (#155).

The tracker URL used to live only in `pyproject.toml`, `manifest.json`, and a
README paragraph — none of which an agent reads mid-failure. These tests pin
one in-band copy per surface (MCP instructions, `cwms://capabilities`, the CLI
`schema` payload) to the `pyproject.toml` `Issues` URL so the copies cannot
drift, and check the guidance tells an agent what is *not* a cwms-tools defect.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from cwms_tools import ISSUES_URL
from cwms_tools.cli.app import app
from cwms_tools.cli.commands.schema import cli_contract_payload
from cwms_tools.mcp.contract import server_instructions
from cwms_tools.mcp.resources import capabilities_payload, capability_contract_payload
from cwms_tools.mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_issues_url_matches_pyproject_and_manifest() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert pyproject["project"]["urls"]["Issues"] == ISSUES_URL
    assert manifest["support"] == ISSUES_URL


def test_capabilities_carry_issues_url_and_guidance() -> None:
    payload = capabilities_payload()
    assert payload["issues_url"] == ISSUES_URL
    guidance = payload["issue_reporting"]
    # What counts as a defect in cwms-tools, and what does not.
    assert "upstream_error" in guidance
    assert "ghost" in guidance.lower()
    assert "active_workarounds" in guidance
    # How to report: offer, don't file unasked; quote the branchable identifiers.
    assert "error.code" in guidance
    assert "error.data.machine_code" in guidance
    assert "request_id" in guidance
    assert "fingerprint" in guidance
    assert "unasked" in guidance
    assert "public" in guidance


def test_issue_reporting_is_fingerprinted_contract_prose() -> None:
    """The guidance changes what an agent does on a failure, so it belongs in
    the fingerprinted contract subset, not only the runtime payload."""
    contract = capability_contract_payload()
    assert contract["issues_url"] == ISSUES_URL
    assert contract["issue_reporting"] == capabilities_payload()["issue_reporting"]


def test_server_instructions_name_the_tracker() -> None:
    text = server_instructions()
    assert ISSUES_URL in text
    assert "issues_url" in text  # points at the capabilities field
    assert "upstream_error" in text  # names the main non-defect


def test_cli_schema_carries_issues_url() -> None:
    result = CliRunner().invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["issues_url"] == ISSUES_URL


def test_cli_contract_fingerprints_issues_url() -> None:
    """Copilot review of #156: the CLI field must sit inside the fingerprinted
    CLI contract subset, so dropping or renaming it moves the fingerprint like
    any other agent-visible surface change."""
    assert cli_contract_payload()["issues_url"] == ISSUES_URL


def test_capabilities_resource_read_carries_issues_url() -> None:
    """What an MCP client sees on `resources/read cwms://capabilities`, through
    the live FastMCP server rather than the payload function."""

    async def go() -> dict:
        result = await build_server().read_resource("cwms://capabilities")
        (item,) = result.contents
        return json.loads(item.content)

    payload = asyncio.run(go())
    assert payload["issues_url"] == ISSUES_URL
    assert "upstream_error" in payload["issue_reporting"]


def test_built_server_instructions_carry_issues_url() -> None:
    """What an MCP client sees in the `initialize` result's `instructions`."""
    assert ISSUES_URL in (build_server().instructions or "")
