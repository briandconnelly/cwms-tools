"""`cwms-tools publisher for-parameter` — bounded publisher-by-parameter index."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.render import emit
from cwms_tools.core import publishers_index

app = typer.Typer(
    name="publisher",
    help=(
        "Queries about CWMS publishers — the operational teams and data "
        "pipelines that produce timeseries (the version segment of a ts_id)."
    ),
    no_args_is_help=True,
)


@app.command("for-parameter")
def for_parameter(
    parameter: Annotated[
        str,
        typer.Argument(help="Parameter code (e.g. Elev, Flow-In, Flow-Out, Stage)."),
    ],
    office: Annotated[
        list[str] | None,
        typer.Option(
            "--office",
            "-o",
            help=(
                "Office code (e.g. NWDM, SWT). Repeat to query several. "
                "If omitted, only offices already in cache are scanned; "
                "the index does not implicitly fan out to every office."
            ),
        ),
    ] = None,
) -> None:
    """List the publishers that report a parameter, with explicit coverage.

    A per-call budget caps how many uncached offices are fetched.
    Offices beyond the budget land in `coverage.offices_skipped_for_budget`;
    rerun this command with those names passed as repeated `--office`
    arguments to continue the index in chunks.
    """
    payload = publishers_index.publishers_for_parameter(
        parameter,
        offices=list(office) if office else None,
    )
    emit(payload)
