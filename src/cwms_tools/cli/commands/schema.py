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
    help=(
        "Print the full machine schema for cwms-tools: command tree with "
        "argument hints, exit codes, symbolic error codes, environment "
        "inputs, MCP tool/resource inventory, and the machine-mode profile. "
        "The fingerprint scope is included; the fingerprint value itself "
        "comes from `cwms-tools fingerprint`."
    ),
)


def _machine_profile() -> dict[str, Any]:
    return {
        "flags": ["--machine", "--json"],
        "auto_enabled_when": "stdout is not a TTY",
        "stdin": "not_read",
        "success_stream": "stdout",
        "error_stream": "stderr",
        "error_shape": "{ok: false, error: {code, message, field?, hint?, repair?, ...}}",
        "error_stream_exceptions": [
            "value get (bulk-result): per-item failures appear inline in the "
            "stdout aggregate under results[].error; the process still exits "
            "non-zero on any item failure. Whole-command usage errors (e.g. a "
            "malformed id) still go to stderr."
        ],
    }


def _opt(
    name: str,
    type_: str,
    *,
    default: Any = None,
    enum: list[str] | None = None,
    required: bool = False,
    repeatable: bool = False,
    help: str = "",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "name": name,
        "type": type_,
        "required": required,
        "repeatable": repeatable,
    }
    if default is not None:
        rec["default"] = default
    if enum is not None:
        rec["enum"] = enum
    if help:
        rec["help"] = help
    return rec


def _arg(
    name: str,
    type_: str,
    *,
    required: bool = True,
    variadic: bool = False,
    help: str = "",
) -> dict[str, Any]:
    return {"name": name, "type": type_, "required": required, "variadic": variadic, "help": help}


def _errs(*codes: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in codes:
        ec = ErrorCode(c)
        out.append({"code": c, "exit": exit_code_for(ec)})
    return out


def _commands() -> list[dict[str, Any]]:
    """Structured command surface. Updates here are part of the capability fingerprint."""
    return [
        # Inspection affordances.
        {
            "path": "cwms-tools whoami",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "cached",
            "arguments": [],
            "options": [],
            "error_codes": [],
        },
        {
            "path": "cwms-tools env",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "local",
            "arguments": [],
            "options": [],
            "error_codes": [],
        },
        {
            "path": "cwms-tools config show",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "local",
            "arguments": [],
            "options": [
                _opt(
                    "--resolved",
                    "boolean",
                    default=False,
                    required=True,
                    help="Show the merged effective configuration.",
                )
            ],
            "error_codes": _errs("usage_error"),
        },
        {
            "path": "cwms-tools fingerprint",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "cached",
            "arguments": [],
            "options": [],
            "error_codes": [],
        },
        {
            "path": "cwms-tools schema",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "local",
            "arguments": [],
            "options": [],
            "error_codes": [],
        },
        # Place tools (M4).
        {
            "path": "cwms-tools place search",
            "output_class": "list",
            "reads_stdin": False,
            "latency_class": "network",
            "arguments": [_arg("query", "string", help="Name fragment, case-insensitive.")],
            "options": [
                _opt("--office", "string", repeatable=True, help="Office code; repeat to fan out."),
                _opt("--parameter", "string", help="Filter to a published parameter."),
                _opt("--limit", "integer", default=50, help="Result cap; 0 = no cap."),
                _opt("--cursor", "string", help="Pagination cursor from prior next_cursor."),
                _opt("--detail", "string", default="summary", enum=["summary", "full"]),
            ],
            "error_codes": _errs(
                "ghost_office", "invalid_cursor", "rate_limited", "upstream_error", "usage_error"
            ),
            "notes": (
                "Default --limit=50 caps result count to keep responses small "
                "on broad queries; pass --limit=0 for no cap. Response carries "
                "`truncated`/`total_count` when the cap is reached."
            ),
        },
        {
            "path": "cwms-tools place describe",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "network",
            "arguments": [_arg("spec", "string", help="OFFICE/NAME, e.g. NWDM/FTPK.")],
            "options": [_opt("--detail", "string", default="summary", enum=["summary", "full"])],
            "error_codes": _errs(
                "ghost_office", "not_found", "rate_limited", "upstream_error", "usage_error"
            ),
        },
        {
            "path": "cwms-tools place parameters",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "network",
            "arguments": [_arg("spec", "string", help="OFFICE/NAME.")],
            "options": [],
            "error_codes": _errs(
                "ghost_office", "not_found", "rate_limited", "upstream_error", "usage_error"
            ),
        },
        # Region browse (M4).
        {
            "path": "cwms-tools region browse",
            "output_class": "list",
            "reads_stdin": False,
            "latency_class": "network",
            "arguments": [],
            "options": [
                _opt("--office", "string", required=True, help="Office code (required)."),
                _opt("--south", "number"),
                _opt("--west", "number"),
                _opt("--north", "number"),
                _opt("--east", "number"),
                _opt("--state", "string", help="Two-letter US state code."),
                _opt("--limit", "integer", default=50, help="Result cap; 0 = no cap."),
                _opt("--cursor", "string", help="Pagination cursor from prior next_cursor."),
            ],
            "error_codes": _errs(
                "ghost_office", "invalid_cursor", "rate_limited", "upstream_error", "usage_error"
            ),
            "notes": (
                "Default --limit=50 caps result count; pass --limit=0 for no cap. "
                "Response carries `truncated`/`total_count`/`truncation_hint` when capped."
            ),
        },
        # Value tools (M5).
        {
            "path": "cwms-tools value get",
            "output_class": "bulk-result",
            "reads_stdin": False,
            "latency_class": "slow",
            "supports_partial_failure": True,
            "partial_failure": "non-zero exit on any item failure; per-item errors inline",
            "arguments": [
                _arg(
                    "id_specs",
                    "string",
                    variadic=True,
                    help="One or more OFFICE/NAME/PARAMETER ids.",
                )
            ],
            "options": [
                _opt("--window-hours", "integer", default=24),
                _opt("--unit", "string", default="EN", enum=["EN", "SI"]),
                _opt(
                    "--with-status",
                    "boolean",
                    default=False,
                    help="Classify against Location Levels (slow; ~8s budget).",
                ),
                _opt("--detail", "string", default="summary", enum=["summary", "full"]),
            ],
            "error_codes": _errs(
                "ghost_office", "not_found", "rate_limited", "upstream_error", "usage_error"
            ),
            "notes": (
                "Threshold classification against CWMS Location Levels is OFF "
                "by default (the /levels endpoint is reliably slow). Pass "
                "--with-status to opt in; the response always carries "
                "`level_lookup_status` so callers can distinguish "
                "skipped/computed/timed_out/unavailable."
            ),
        },
        {
            "path": "cwms-tools value history",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "slow",
            "arguments": [_arg("id_spec", "string", help="OFFICE/NAME/PARAMETER.")],
            "options": [
                _opt("--begin", "string", required=True, help="RFC3339 window start."),
                _opt("--end", "string", required=True, help="RFC3339 window end."),
                _opt("--unit", "string", default="EN", enum=["EN", "SI"]),
                _opt("--detail", "string", default="summary", enum=["summary", "full"]),
            ],
            "error_codes": _errs(
                "ghost_office",
                "invalid_field",
                "not_found",
                "rate_limited",
                "upstream_error",
                "usage_error",
            ),
        },
        # Publisher index (M6).
        {
            "path": "cwms-tools publisher for-parameter",
            "output_class": "record",
            "reads_stdin": False,
            "latency_class": "network",
            "arguments": [_arg("parameter", "string", help="Parameter code, e.g. Elev.")],
            "options": [_opt("--office", "string", repeatable=True, help="Office code; repeat.")],
            "error_codes": _errs("rate_limited", "upstream_error"),
        },
        # MCP server (M7).
        {
            "path": "cwms-tools mcp serve",
            "output_class": "stream",
            "reads_stdin": True,
            "latency_class": "async",
            "arguments": [],
            "options": [
                _opt(
                    "--transport",
                    "string",
                    default="stdio",
                    enum=["stdio", "streamable-http"],
                ),
                _opt("--host", "string", default="127.0.0.1"),
                _opt("--port", "integer", default=8765),
            ],
            "error_codes": _errs("usage_error"),
            "notes": "stdio transport reserves stdout for the JSON-RPC channel",
        },
    ]


def cli_contract_payload() -> dict[str, Any]:
    """Fingerprint-relevant CLI contract subset (no fingerprint value, to avoid recursion)."""
    return {
        "commands": _commands(),
        "exit_codes": _exit_codes(),
        "machine_profile": _machine_profile(),
    }


def _schema_payload() -> dict[str, Any]:
    contract = cli_contract_payload()
    return {
        "name": "cwms-tools",
        "version": PKG_VERSION,
        "fingerprint_scope": fp.FINGERPRINT_SCOPE,
        "commands": contract["commands"],
        "exit_codes": contract["exit_codes"],
        "error_codes": sorted(c.value for c in ErrorCode),
        "env_inputs": _env_inputs(),
        "mcp_tools": TOOL_INVENTORY,
        "mcp_resources": RESOURCE_INVENTORY,
        "machine_profile": contract["machine_profile"],
    }


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
    """Print the full machine schema as JSON."""
    emit(_schema_payload())
