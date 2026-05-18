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

    Bounded by a per-call budget: offices that exceed the budget land in
    `coverage.offices_skipped_for_budget` with a `repair` hint pointing
    back at this command so you can continue the index in chunks.
    """
    payload = publishers_index.publishers_for_parameter(
        parameter,
        offices=list(office) if office else None,
    )
    emit(payload)
