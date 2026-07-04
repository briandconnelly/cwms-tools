"""Output rendering for the CLI.

`--machine` and non-TTY stdout produce stable, deterministic JSON on stdout
and route diagnostics to stderr; TTY mode allows pretty-print and rich
formatting (kept minimal for v0.1.0).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NoReturn

import typer

from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    RepairHint,
    exit_code_for,
    surface_field_name,
)
from cwms_tools.core.offices import ghost_office_repair, nw_rollup_target
from cwms_tools.core.rounding import round_floats

# Per `agent-friendly-cli` §"Agent-Safe Invocation": machine mode is forced on
# whenever stdout is not a TTY, regardless of flags. Explicit `--machine` /
# `--json` flags also force it on.

_state: dict[str, bool] = {"machine": False, "isolated": False, "no_cache": False}


def set_machine(value: bool) -> None:
    _state["machine"] = bool(value)


def set_isolated(value: bool) -> None:
    _state["isolated"] = bool(value)
    if value:
        # Mark for downstream consumers (cache/session).
        os.environ["_CWMS_TOOLS_ISOLATED"] = "1"
        os.environ["_CWMS_TOOLS_NO_CACHE"] = "1"


def set_no_cache(value: bool) -> None:
    _state["no_cache"] = bool(value)
    if value:
        os.environ["_CWMS_TOOLS_NO_CACHE"] = "1"


class OutputMode:
    """Resolved output mode for a single CLI invocation."""

    def __init__(self, *, machine: bool | None = None, json_only: bool = False) -> None:
        flag_set = _state["machine"] if machine is None else bool(machine)
        self.machine = flag_set or json_only or not sys.stdout.isatty()
        self.json_only = json_only or self.machine

    @property
    def pretty(self) -> bool:
        return not self.machine

    @property
    def indent(self) -> int | None:
        """JSON indent: pretty in TTY mode, compact in machine mode."""
        return 2 if self.pretty else None


def emit(value: Any, *, mode: OutputMode | None = None) -> None:
    """Print a Python value to stdout as JSON in the resolved mode.

    Use this for every success payload — stdout is success only.
    """
    out_mode = mode or OutputMode()
    indent = out_mode.indent
    # Round float values to strip unit-conversion noise (issue #45). The CLI
    # serializes core dicts straight to JSON, bypassing the Pydantic models, so
    # this is the CLI-side twin of the rounding in `core._compact.CompactDumpMixin`.
    typer.echo(
        json.dumps(round_floats(value), indent=indent, sort_keys=False, default=str),
        nl=True,
    )


def diagnostic(message: str) -> None:
    """Write a single diagnostic line to stderr."""
    typer.echo(message, err=True)


def emit_error(error: CwmsToolsError) -> NoReturn:
    """Write a structured error envelope to STDERR and exit with its mapped code.

    Per `agent-friendly-cli`, stdout stays success-only; failures go to stderr
    as the full `{ok: false, error: {...}}` envelope (the same shape the MCP
    surface returns), branchable by the symbolic `error.code` and the numeric
    exit code. Every CLI command routes whole-command failures through here so
    the error shape and stream are uniform. Also where `field` gets translated
    to the CLI-facing flag name (#68), mirroring `mcp.tools.stamp_envelope` —
    core producers emit whatever internal name suits their own domain.
    """
    if error.envelope.details is not None:
        error.envelope.details.field = surface_field_name(error.envelope.details.field)
    payload = {"ok": False, "error": error.envelope.model_dump(mode="json")}
    typer.echo(json.dumps(payload, default=str), err=True)
    raise typer.Exit(code=exit_code_for(error.envelope.code))


def rewrite_error_field(error: CwmsToolsError, *, when: str, to: str) -> CwmsToolsError:
    """Override `error.envelope.field` to `to` when it currently equals `when`.

    `emit_error`'s `surface_field_name()` translation assumes a flag exists
    for the producer-internal name it's correcting to (true for MCP tools
    and CLI commands with a real `--office` flag). Some CLI commands take a
    combined positional instead (`place describe/parameters`'s `OFFICE/NAME`
    `spec`; `value get/history/profile`'s `OFFICE/NAME/PARAMETER` id) — for
    those, `office`/`office_id` isn't a real argument on the command at all,
    so the surface-facing `field` needs command-specific redirection to the
    positional the caller can actually retry with (#68 review feedback).
    Call this BEFORE `emit_error`/serialization so `surface_field_name`'s
    default translation doesn't run first (it's a no-op on an already-
    rewritten name, but checking the untranslated producer name here keeps
    the intent obvious at each call site).
    """
    if error.envelope.details is not None and error.envelope.details.field == when:
        error.envelope.details.field = to
    return error


def attach_ghost_office_repair(
    error: CwmsToolsError, *, tool: str, args: dict[str, Any]
) -> CwmsToolsError:
    """If `error` is `ghost_office`, attach a same-command retry repair.

    `tool`/`args` are THIS command's own name and original (wire-format,
    `--flag`-ready) arguments minus `office` — core no longer hardcodes a
    same-tool-switching repair (it doesn't know which command is calling),
    so each CLI command supplies its own identity here, mirroring
    `mcp.tools._safe`'s `_repair_call` (#69). No-op for any other error code.
    Independent of `rewrite_error_field` (that touches `field`; this touches
    `repair`) — call in either order, but before `emit_error`, which
    serializes the envelope and exits.
    """
    office_id = error.envelope.details.value if error.envelope.details else None
    if error.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
        error.envelope.repair = ghost_office_repair(office_id, tool=tool, args=args)
    return error


def attach_ghost_office_spec_repair(
    error: CwmsToolsError, *, tool: str, spec_key: str, spec_suffix: str, args: dict[str, Any]
) -> CwmsToolsError:
    """Like `attach_ghost_office_repair`, but for commands with a combined
    `OFFICE/...` positional instead of a separate `--office` flag (`place
    describe`/`parameters`'s `spec`; `value get`/`history`/`profile`'s
    `id_specs`/`id_spec`) — `spec_key` names that positional and
    `spec_suffix` is everything after `OFFICE/` (e.g. `NAME` or
    `NAME/PARAMETER`); the repaired value becomes `{target}/{spec_suffix}`.
    """
    office_id = error.envelope.details.value if error.envelope.details else None
    if error.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
        target = nw_rollup_target(office_id)
        error.envelope.repair = RepairHint(
            next_step="retry_with_rollup_office",
            tool=tool,
            arguments={**args, spec_key: f"{target}/{spec_suffix}"},
        )
    return error


def isolated() -> bool:
    """Return True if the caller asked to bypass on-disk cache + env reads."""
    return _state["isolated"] or os.environ.get("_CWMS_TOOLS_ISOLATED") == "1"


def no_cache() -> bool:
    """Return True if the caller asked to bypass on-disk cache."""
    return _state["no_cache"] or _state["isolated"] or os.environ.get("_CWMS_TOOLS_NO_CACHE") == "1"


__all__ = [
    "OutputMode",
    "attach_ghost_office_repair",
    "attach_ghost_office_spec_repair",
    "diagnostic",
    "emit",
    "emit_error",
    "isolated",
    "no_cache",
    "rewrite_error_field",
    "set_isolated",
    "set_machine",
    "set_no_cache",
]
