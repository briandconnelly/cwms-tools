"""`cwms-tools region browse` — enriched catalog browse filtered by office/bbox/state."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.exit_codes import from_error_code
from cwms_tools.cli.render import emit
from cwms_tools.core import places
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.geo import BBox

app = typer.Typer(
    name="region",
    help=("Browse the catalog of one office, optionally filtered by bounding box and/or US state."),
    no_args_is_help=True,
)


@app.command("browse")
def browse(
    office: Annotated[
        str,
        typer.Option(
            "--office",
            "-o",
            help="USACE office code (e.g. NWDM, SWT). Required.",
        ),
    ],
    south: Annotated[
        float | None,
        typer.Option("--south", help="Bounding box south latitude, decimal degrees."),
    ] = None,
    west: Annotated[
        float | None,
        typer.Option("--west", help="Bounding box west longitude, decimal degrees."),
    ] = None,
    north: Annotated[
        float | None,
        typer.Option("--north", help="Bounding box north latitude, decimal degrees."),
    ] = None,
    east: Annotated[
        float | None,
        typer.Option("--east", help="Bounding box east longitude, decimal degrees."),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Two-letter US state code (e.g. MT, OK)."),
    ] = None,
) -> None:
    """Browse the locations published by one office, with optional filters.

    All four bounding-box corners must be set together or none. When
    --state and a bbox are both set, both filters apply. Returns the same
    enriched per-place records as `place search`, with `result_count`
    and `ghost_count` totals at the top.
    """
    provided = [v for v in (south, west, north, east) if v is not None]
    if len(provided) not in {0, 4}:
        emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.USAGE_ERROR.value,
                    "message": (
                        "When specifying a bounding box, --south, --west, --north, "
                        "--east must all be provided."
                    ),
                    "field": "bbox",
                },
            }
        )
        raise typer.Exit(code=2)

    bbox: BBox | None = None
    if south is not None and west is not None and north is not None and east is not None:
        bbox = BBox(south=south, west=west, north=north, east=east)

    try:
        emit(places.browse_region(office=office, bbox=bbox, state=state))
    except CwmsToolsError as err:
        emit({"ok": False, "error": err.envelope.model_dump(mode="json")})
        raise typer.Exit(code=from_error_code(err.envelope.code)) from err
