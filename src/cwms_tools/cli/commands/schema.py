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


def _commands() -> list[dict[str, Any]]:
    """v0.1.0 command surface. Updates here are part of the capability fingerprint."""
    return [
        # Inspection affordances.
        {"path": "cwms-tools whoami", "output_class": "record", "reads_stdin": False},
        {"path": "cwms-tools env", "output_class": "record", "reads_stdin": False},
        {
            "path": "cwms-tools config show --resolved",
            "output_class": "record",
            "reads_stdin": False,
        },
        {"path": "cwms-tools fingerprint", "output_class": "record", "reads_stdin": False},
        {"path": "cwms-tools schema", "output_class": "record", "reads_stdin": False},
        # Place tools (M4).
        {
            "path": "cwms-tools place search <query> --office <office>",
            "output_class": "list",
            "reads_stdin": False,
        },
        {
            "path": "cwms-tools place describe <office>/<name>",
            "output_class": "record",
            "reads_stdin": False,
        },
        {
            "path": "cwms-tools place parameters <office>/<name>",
            "output_class": "record",
            "reads_stdin": False,
        },
        # Region browse (M4).
        {
            "path": (
                "cwms-tools region browse --office <office> "
                "[--south N --west N --north N --east N] [--state XX]"
            ),
            "output_class": "list",
            "reads_stdin": False,
        },
        # Value tools (M5).
        {
            "path": (
                "cwms-tools value get <office>/<name>/<param>... "
                "[--window-hours N] [--unit EN|SI] [--detail summary|full]"
            ),
            "output_class": "bulk-result",
            "reads_stdin": False,
            "supports_partial_failure": True,
            "partial_failure": "non-zero exit on any item failure; per-item errors inline",
        },
        {
            "path": (
                "cwms-tools value history <office>/<name>/<param> "
                "--begin <RFC3339> --end <RFC3339> [--unit EN|SI] [--detail summary|full]"
            ),
            "output_class": "record",
            "reads_stdin": False,
        },
        # Publisher index (M6).
        {
            "path": "cwms-tools publisher for-parameter <param> [--office X]*",
            "output_class": "record",
            "reads_stdin": False,
        },
        # MCP server (M7).
        {
            "path": (
                "cwms-tools mcp serve --transport stdio|streamable-http [--host H] [--port P]"
            ),
            "output_class": "stream",
            "reads_stdin": True,
            "notes": "stdio transport reserves stdout for the JSON-RPC channel",
        },
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
