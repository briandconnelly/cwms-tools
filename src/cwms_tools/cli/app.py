"""Main Typer application. Subcommands are registered in `commands/`."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools import __version__
from cwms_tools.cli.commands import config as config_cmd
from cwms_tools.cli.commands import env as env_cmd
from cwms_tools.cli.commands import fingerprint as fingerprint_cmd
from cwms_tools.cli.commands import mcp as mcp_cmd
from cwms_tools.cli.commands import place as place_cmd
from cwms_tools.cli.commands import publisher as publisher_cmd
from cwms_tools.cli.commands import region as region_cmd
from cwms_tools.cli.commands import schema as schema_cmd
from cwms_tools.cli.commands import value as value_cmd
from cwms_tools.cli.commands import whoami as whoami_cmd
from cwms_tools.cli.render import set_isolated, set_machine, set_no_cache


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cwms-tools {__version__}")
        raise typer.Exit(code=0)


app = typer.Typer(
    name="cwms-tools",
    help=(
        "Read-only CLI and MCP server for the USACE CWMS Data API. "
        "Task-completing commands (search a place, get current value with "
        "status context, browse an office's catalog) and an MCP server "
        "(`cwms-tools mcp serve`) share one behavioral core."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback(invoke_without_command=True)
def _root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Print the cwms-tools version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
    machine: Annotated[
        bool,
        typer.Option(
            "--machine",
            help=(
                "Machine-readable output: compact JSON on stdout, no color, "
                "no progress indicators, no interactive prompts. "
                "Auto-enabled when stdout is not a terminal."
            ),
        ),
    ] = False,
    json_flag: Annotated[
        bool,
        typer.Option(
            "--json",
            help=(
                "Alias for --machine. Provided so callers used to the convention "
                "of passing --json can use it without learning a new flag."
            ),
        ),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help=(
                "Bypass the on-disk catalog cache for this invocation. "
                "Environment variables and resolved session config still apply."
            ),
        ),
    ] = False,
    isolated: Annotated[
        bool,
        typer.Option(
            "--isolated",
            help=(
                "Bypass on-disk cache and ignore CWMS_TOOLS_* environment "
                "variables. Useful for reproducibility checks."
            ),
        ),
    ] = False,
) -> None:
    """Root command. Subcommands listed below; -h on any subcommand for details."""
    if machine or json_flag:
        set_machine(True)
    if no_cache:
        set_no_cache(True)
    if isolated:
        set_isolated(True)


# Inspection affordances required by agent-friendly-cli when ambient state is read.
app.add_typer(whoami_cmd.app, name="whoami")
app.add_typer(env_cmd.app, name="env")
app.add_typer(config_cmd.app, name="config")
app.add_typer(fingerprint_cmd.app, name="fingerprint")
app.add_typer(schema_cmd.app, name="schema")

# Place / region task tools (M4).
app.add_typer(place_cmd.app, name="place")
app.add_typer(region_cmd.app, name="region")

# Value task tools (M5).
app.add_typer(value_cmd.app, name="value")

# Publisher index helper (M6).
app.add_typer(publisher_cmd.app, name="publisher")

# MCP server (M7).
app.add_typer(mcp_cmd.app, name="mcp")


if __name__ == "__main__":  # pragma: no cover
    app()
