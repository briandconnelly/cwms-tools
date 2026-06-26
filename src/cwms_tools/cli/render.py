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

from cwms_tools.core.errors import CwmsToolsError, exit_code_for
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
    the error shape and stream are uniform.
    """
    payload = {"ok": False, "error": error.envelope.model_dump(mode="json")}
    typer.echo(json.dumps(payload, default=str), err=True)
    raise typer.Exit(code=exit_code_for(error.envelope.code))


def isolated() -> bool:
    """Return True if the caller asked to bypass on-disk cache + env reads."""
    return _state["isolated"] or os.environ.get("_CWMS_TOOLS_ISOLATED") == "1"


def no_cache() -> bool:
    """Return True if the caller asked to bypass on-disk cache."""
    return _state["no_cache"] or _state["isolated"] or os.environ.get("_CWMS_TOOLS_NO_CACHE") == "1"


__all__ = [
    "OutputMode",
    "diagnostic",
    "emit",
    "emit_error",
    "isolated",
    "no_cache",
    "set_isolated",
    "set_machine",
    "set_no_cache",
]
