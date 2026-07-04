"""Tests for the M3 CLI inspection affordances (`whoami`, `env`, `config`,
`fingerprint`, `schema`)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.cli.commands.schema import _schema_payload

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


def test_schema_commands_are_structured() -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    cmd = {c["path"]: c for c in doc["commands"]}["cwms-tools place search"]
    names = {o["name"] for o in cmd["options"]}
    assert {"--office", "--limit", "--cursor", "--detail"} <= names
    limit_opt = next(o for o in cmd["options"] if o["name"] == "--limit")
    assert limit_opt["type"] == "integer" and limit_opt["default"] == 50
    detail_opt = next(o for o in cmd["options"] if o["name"] == "--detail")
    assert detail_opt["enum"] == ["summary", "full"]
    office_opt = next(o for o in cmd["options"] if o["name"] == "--office")
    assert office_opt["repeatable"] is True
    assert "invalid_cursor" in {e["code"] for e in cmd["error_codes"]}
    assert cmd["latency_class"] in {"local", "cached", "network", "slow", "async"}


def test_schema_value_get_marks_with_status_slow_path() -> None:
    result = runner.invoke(app, ["schema"])
    doc = json.loads(result.stdout)
    cmd = {c["path"]: c for c in doc["commands"]}["cwms-tools value get"]
    assert cmd["latency_class"] in {"network", "slow"}
    ws = next(o for o in cmd["options"] if o["name"] == "--with-status")
    assert ws["type"] == "boolean"


def _leaf_commands(command: Any, prefix: list[str]) -> list[tuple[list[str], Any]]:
    """Recursively collect (path parts, click Command) for every leaf command."""
    sub = getattr(command, "commands", None)
    if sub:
        out: list[tuple[list[str], Any]] = []
        for name, child in sub.items():
            out.extend(_leaf_commands(child, [*prefix, name]))
        return out
    return [(prefix, command)]


def test_every_typer_command_has_a_schema_entry_with_matching_options() -> None:
    """Regression for #84: a real Typer command missing from `schema._commands()`,
    or a schema entry whose options drift from the real Typer options, must fail
    this test rather than ship silently."""
    click_root = typer.main.get_command(app)
    schema_by_path = {c["path"]: c for c in _schema_payload()["commands"]}

    for name_parts, command in _leaf_commands(click_root, []):
        path = "cwms-tools " + " ".join(name_parts)
        assert path in schema_by_path, f"{path} has no `cwms-tools schema` entry (#84)"
        entry = schema_by_path[path]
        schema_option_names = {o["name"] for o in entry["options"]}

        real_option_names = {p.opts[0] for p in command.params if p.param_type_name == "option"}
        missing = real_option_names - schema_option_names
        assert not missing, f"{path}: schema is missing real options {missing} (#84)"

        stale = schema_option_names - real_option_names
        assert not stale, f"{path}: schema advertises options that don't exist: {stale}"
