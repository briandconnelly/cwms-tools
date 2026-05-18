"""`cwms-tools publisher for-parameter` — bounded publisher-by-parameter index."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.render import emit
from cwms_tools.core import publishers_index

app = typer.Typer(
    name="publisher",
    help="Publisher-related queries (§9.8).",
    no_args_is_help=True,
)


@app.command("for-parameter")
def for_parameter(
    parameter: Annotated[
        str,
        typer.Argument(help="Parameter code (e.g. Elev, Flow-Out)."),
    ],
    office: Annotated[
        list[str] | None,
        typer.Option(
            "--office",
            "-o",
            help="Limit to these offices; can repeat. Defaults to cached offices.",
        ),
    ] = None,
) -> None:
    """List publishers reporting a parameter across the requested offices.

    Without `--office`, the index uses only offices already cached locally —
    we deliberately do NOT fan out to all 68 offices implicitly.
    """
    payload = publishers_index.publishers_for_parameter(
        parameter,
        offices=list(office) if office else None,
    )
    emit(payload)
