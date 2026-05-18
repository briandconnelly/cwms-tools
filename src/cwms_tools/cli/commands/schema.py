"""`cwms-tools schema` — emit the full machine schema.

The schema is the agent-facing contract: command tree, flags, output classes,
exit codes, error codes, declared env inputs, fingerprint scope. It is a
snapshot in M2-M3; later milestones extend it as commands land.
"""

from __future__ import annotations

from typing import Any

import typer

from cwms_tools import __version__ as PKG_VERSION
from cwms_tools.cli.commands.env import READ_VARS, SECRET_VARS
from cwms_tools.cli.render import emit
from cwms_tools.core import fingerprint as fp
from cwms_tools.core.errors import ErrorCode, exit_code_for
from cwms_tools.mcp.resources import RESOURCE_INVENTORY, TOOL_INVENTORY

app = typer.Typer(
    name="schema",
    help="Emit the full machine schema (command tree, errors, env inputs, fingerprint).",
)


def _schema_payload() -> dict[str, Any]:
    return {
        "name": "cwms-tools",
        "version": PKG_VERSION,
        "fingerprint_scope": fp.FINGERPRINT_SCOPE,
        "commands": _commands(),
        "exit_codes": _exit_codes(),
        "error_codes": sorted(c.value for c in ErrorCode),
        "env_inputs": _env_inputs(),
        "mcp_tools": TOOL_INVENTORY,
        "mcp_resources": RESOURCE_INVENTORY,
        "machine_profile": {
            "flags": ["--machine", "--json"],
            "auto_enabled_when": "stdout is not a TTY",
            "stdin": "not_read",
        },
    }


def _commands() -> list[dict[str, str]]:
    """v0.1.0 command surface; extended by later milestones."""
    return [
        {"path": "cwms-tools whoami", "output_class": "record"},
        {"path": "cwms-tools env", "output_class": "record"},
        {"path": "cwms-tools config show --resolved", "output_class": "record"},
        {"path": "cwms-tools fingerprint", "output_class": "record"},
        {"path": "cwms-tools schema", "output_class": "record"},
    ]


def _exit_codes() -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = [
        {"code": "ok", "exit": 0},
        {"code": "generic_error", "exit": 1},
    ]
    rows.extend({"code": ec.value, "exit": exit_code_for(ec)} for ec in ErrorCode)
    return rows


def _env_inputs() -> list[dict[str, str | bool]]:
    return [{"name": name, "secret": name in SECRET_VARS} for name in READ_VARS]


@app.callback(invoke_without_command=True)
def schema_cmd() -> None:
    """Emit the full machine schema."""
    emit(_schema_payload())
