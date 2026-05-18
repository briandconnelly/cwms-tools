"""Output rendering for the CLI.

`--machine` and non-TTY stdout produce stable, deterministic JSON on stdout
and route diagnostics to stderr; TTY mode allows pretty-print and rich
formatting (kept minimal for v0.1.0).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import typer

# Per `agent-friendly-cli` §"Agent-Safe Invocation": machine mode is forced on
# whenever stdout is not a TTY, regardless of flags. Explicit `--machine` /
# `--json` flags also force it on.


class OutputMode:
    """Resolved output mode for a single CLI invocation."""

    def __init__(self, *, machine: bool = False, json_only: bool = False) -> None:
        self.machine = machine or json_only or not sys.stdout.isatty()
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
    typer.echo(
        json.dumps(value, indent=indent, sort_keys=False, default=str),
        nl=True,
    )


def diagnostic(message: str) -> None:
    """Write a single diagnostic line to stderr."""
    typer.echo(message, err=True)


def isolated() -> bool:
    """Return True if the caller asked to bypass on-disk cache + env reads."""
    return os.environ.get("_CWMS_TOOLS_ISOLATED") == "1"


__all__ = ["OutputMode", "diagnostic", "emit", "isolated"]
