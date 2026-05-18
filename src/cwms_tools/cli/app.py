"""Main Typer application. Subcommands are registered in `commands/`."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools import __version__
from cwms_tools.cli.commands import config as config_cmd
from cwms_tools.cli.commands import env as env_cmd
from cwms_tools.cli.commands import fingerprint as fingerprint_cmd
from cwms_tools.cli.commands import place as place_cmd
from cwms_tools.cli.commands import publisher as publisher_cmd
from cwms_tools.cli.commands import region as region_cmd
from cwms_tools.cli.commands import schema as schema_cmd
from cwms_tools.cli.commands import value as value_cmd
from cwms_tools.cli.commands import whoami as whoami_cmd


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cwms-tools {__version__}")
        raise typer.Exit(code=0)


app = typer.Typer(
    name="cwms-tools",
    help=(
        "Agent-friendly tools for the USACE CWMS Data API. "
        "Provides task-completing commands and an MCP server "
        "(`cwms-tools mcp serve`) over a shared behavioral core."
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
) -> None:
    """Root callback. Subcommands land in later milestones."""


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


if __name__ == "__main__":  # pragma: no cover
    app()
