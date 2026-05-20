"""Tests for the M3 CLI inspection affordances (`whoami`, `env`, `config`,
`fingerprint`, `schema`)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app

runner = CliRunner()


def _invoke_json(args: list[str], env: dict[str, str] | None = None) -> dict:
    result = runner.invoke(app, args, env=env or {})
    assert result.exit_code in {0, 2}, result.stdout + "\n" + (result.stderr or "")
    return json.loads(result.stdout)


def test_whoami_emits_anonymous_identity_in_v0_1_0() -> None:
    payload = _invoke_json(["whoami"])
    assert payload["identity"] == "anonymous"
    assert payload["api_root"].startswith("https://")
    assert "cwms-tools/" in payload["user_agent"]


def test_env_lists_every_declared_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CWMS_TOOLS_USER_AGENT_EXTRA", "test-run")
    payload = _invoke_json(["env"])
    names = {row["name"] for row in payload["variables"]}
    assert "CWMS_TOOLS_API_ROOT" in names
    assert "CWMS_TOOLS_CACHE_DIR" in names
    assert "CWMS_API_KEY" in names  # declared even if unused in v0.1.0
    # The extra we set must show up as set=true.
    extra = next(r for r in payload["variables"] if r["name"] == "CWMS_TOOLS_USER_AGENT_EXTRA")
    assert extra["set"] == "true"
    assert extra["value"] == "test-run"


def test_env_redacts_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CWMS_API_KEY", "abcdef1234567890")
    payload = _invoke_json(["env"])
    row = next(r for r in payload["variables"] if r["name"] == "CWMS_API_KEY")
    assert row["secret"] == "true"
    assert row["value"] is not None
    assert row["value"].startswith("***")
    # The raw secret must never appear in the rendered output.
    assert "abcdef1234567890" not in json.dumps(payload)


def test_config_show_requires_resolved_flag() -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 2  # usage_error
    assert result.stdout == ""  # stdout stays success-only; error goes to stderr
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"


def test_config_show_resolved_emits_effective_config() -> None:
    payload = _invoke_json(["config", "show", "--resolved"])
    assert "api_root" in payload
    assert "cache_dir" in payload
    assert payload["workers"] >= 1
    assert payload["env_inputs_read"]  # non-empty


def test_fingerprint_emits_64_hex_digest() -> None:
    payload = _invoke_json(["fingerprint"])
    digest = payload["fingerprint"]
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert payload["scope"] == "schema-contract"


def test_schema_emits_command_tree_and_exit_map() -> None:
    payload = _invoke_json(["schema"])
    assert payload["name"] == "cwms-tools"
    commands = {c["path"] for c in payload["commands"]}
    assert "cwms-tools whoami" in commands
    assert "cwms-tools schema" in commands
    exit_codes = {row["code"]: row["exit"] for row in payload["exit_codes"]}
    assert exit_codes["ghost_office"] == 12
    assert exit_codes["rate_limited"] == 6
    # MCP surface is mirrored:
    assert "cwms_get_overview_section" in payload["mcp_tools"]


def test_schema_is_stable_across_invocations() -> None:
    """Snapshot-style check: two invocations produce byte-identical output."""
    one = runner.invoke(app, ["schema"]).stdout
    two = runner.invoke(app, ["schema"]).stdout
    assert one == two
