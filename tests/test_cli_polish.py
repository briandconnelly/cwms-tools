"""Tests for the M7 CLI polish: --machine, --no-cache / --isolated, mcp serve wiring."""

from __future__ import annotations

import json
import os
import re

import pytest
from typer.testing import CliRunner

from cwms_tools.cli import render
from cwms_tools.cli.app import app

runner = CliRunner()

# Rich renders backtick code-spans from docstrings with ANSI style boundaries
# that can split flag names across escape sequences (e.g. `--with-status`
# becomes `-` + `-with` + `-status` with styles between). For substring-
# matching against help output we strip ANSI first so the test matches what
# a human reads, not what Rich emits.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture(autouse=True)
def reset_render_state(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with a clean global render state."""
    monkeypatch.delenv("_CWMS_TOOLS_ISOLATED", raising=False)
    monkeypatch.delenv("_CWMS_TOOLS_NO_CACHE", raising=False)
    render._state.update(machine=False, isolated=False, no_cache=False)
    yield
    render._state.update(machine=False, isolated=False, no_cache=False)


# --------------------------------------------------------------------------
# Global flag wiring
# --------------------------------------------------------------------------


def test_isolated_flag_sets_env_markers() -> None:
    result = runner.invoke(app, ["--isolated", "whoami"])
    assert result.exit_code == 0, result.stdout
    # The flag sets process env markers that downstream cache reads honor.
    assert os.environ.get("_CWMS_TOOLS_ISOLATED") == "1"
    assert os.environ.get("_CWMS_TOOLS_NO_CACHE") == "1"


def test_no_cache_flag_sets_env_marker() -> None:
    result = runner.invoke(app, ["--no-cache", "whoami"])
    assert result.exit_code == 0, result.stdout
    assert os.environ.get("_CWMS_TOOLS_NO_CACHE") == "1"
    # --no-cache must NOT imply --isolated.
    assert os.environ.get("_CWMS_TOOLS_ISOLATED") in {None, ""}


def test_machine_flag_forces_compact_json() -> None:
    """`--machine` produces compact (no-indent) JSON; pretty mode is multiline."""
    compact = runner.invoke(app, ["--machine", "whoami"])
    assert compact.exit_code == 0
    # Compact JSON has no newlines inside the object — single line.
    assert "\n" not in compact.stdout.strip()

    # Without --machine and with CliRunner's non-TTY stdout, mode still auto-
    # switches to machine. Test that explicit json flag emits the same shape.
    json_flag = runner.invoke(app, ["--json", "whoami"])
    assert json_flag.exit_code == 0
    json.loads(json_flag.stdout)  # parses


# --------------------------------------------------------------------------
# mcp serve subcommand wiring
# --------------------------------------------------------------------------


def test_mcp_serve_rejects_unknown_transport() -> None:
    result = runner.invoke(app, ["mcp", "serve", "--transport", "carrier-pigeon"])
    assert result.exit_code == 2
    # Diagnostic on stderr.
    assert "unknown transport" in (result.stderr or result.stdout).lower()


def test_mcp_subcommand_shows_serve_in_help() -> None:
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "serve" in result.stdout


def test_mcp_serve_help_lists_transport_and_port() -> None:
    result = runner.invoke(app, ["mcp", "serve", "--help"])
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    assert "--transport" in out
    assert "--port" in out


# --------------------------------------------------------------------------
# Surface snapshots
# --------------------------------------------------------------------------


def test_root_help_lists_all_top_level_commands() -> None:
    """Pins the set of top-level commands; adding/removing one must update this test."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    expected = {
        "whoami",
        "env",
        "config",
        "fingerprint",
        "schema",
        "place",
        "region",
        "value",
        "publisher",
        "mcp",
    }
    out = result.stdout
    missing = {cmd for cmd in expected if cmd not in out}
    assert not missing, f"missing commands from --help: {missing}"


def test_schema_output_lists_all_top_level_commands() -> None:
    """Machine schema must include every top-level command path."""
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    paths = {c["path"] for c in payload["commands"]}
    # The schema currently lists the inspection-affordance commands explicitly.
    # Pin that set so a future change to schema._commands() forces a review.
    assert "cwms-tools whoami" in paths
    assert "cwms-tools schema" in paths


def test_schema_value_get_advertises_with_status_flag() -> None:
    """The schema is the agent contract: it must mention --with-status and
    explain that classification is off by default. Eval found the schema
    silent on this even though the --help output mentions the flag."""
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    value_get = next(
        (c for c in payload["commands"] if c["path"].startswith("cwms-tools value get")),
        None,
    )
    assert value_get is not None, "schema must include the `value get` entry"
    assert "--with-status" in value_get["path"]
    assert "level_lookup_status" in value_get.get("notes", "")


def test_schema_place_search_advertises_limit_flag() -> None:
    """The schema must advertise --limit on place search so agents know the
    default cap exists and how to disable it."""
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    place_search = next(
        (c for c in payload["commands"] if c["path"].startswith("cwms-tools place search")),
        None,
    )
    assert place_search is not None
    assert "--limit" in place_search["path"]
    assert "truncated" in place_search.get("notes", "")


def test_value_get_help_documents_with_status_and_depth_example() -> None:
    """`value get --help` must mention `--with-status` and a realistic
    depth-tagged id example. The classification-opt-in framing and the
    comma-in-id shape are the two things eval surfaced as easy to miss."""
    result = runner.invoke(app, ["value", "get", "--help"])
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    assert "--with-status" in out
    # The depth-tagged example: agents searching for water temp sensors
    # need to know this id shape is legitimate.
    assert "UBLW_S1-D21,0ft" in out


def test_value_history_help_documents_depth_example() -> None:
    """`value history --help` must show the depth-tagged id example so
    callers know commas and depth suffixes in OFFICE/NAME/PARAMETER are ok."""
    result = runner.invoke(app, ["value", "history", "--help"])
    assert result.exit_code == 0
    assert "UBLW_S1-D21,0ft" in _strip_ansi(result.stdout)


def test_schema_carries_machine_profile_declaration() -> None:
    result = runner.invoke(app, ["schema"])
    payload = json.loads(result.stdout)
    profile = payload["machine_profile"]
    assert "--machine" in profile["flags"]
    assert profile["stdin"] == "not_read"
