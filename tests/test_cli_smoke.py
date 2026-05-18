"""Smoke tests for the Typer CLI scaffold.

The full command surface lands in M3-M7; these tests pin the entry-point
shape so we catch regressions in the console_scripts wiring early.
"""

from __future__ import annotations

from typer.testing import CliRunner

from cwms_tools import __version__
from cwms_tools.cli.app import app

runner = CliRunner()


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_short_version_flag_works() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_renders_without_crashing() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "cwms-tools" in result.stdout.lower()


def test_no_args_shows_help() -> None:
    """`no_args_is_help=True` on the root app surfaces help when invoked bare."""
    result = runner.invoke(app, [])
    # Typer exits with code 0 or 2 depending on version; both are valid here.
    assert result.exit_code in {0, 2}
    assert "cwms-tools" in result.stdout.lower() or "cwms-tools" in result.stderr.lower()
