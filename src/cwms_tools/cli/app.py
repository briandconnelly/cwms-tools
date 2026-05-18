"""Main Typer application. Subcommands are registered in `commands/`."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools import __version__


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


if __name__ == "__main__":  # pragma: no cover
    app()
