"""`cwms-tools region browse` — enriched catalog browse filtered by office/bbox/state."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.render import emit, emit_error
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
        typer.Option(
            "--south",
            help=(
                "Bounding box south latitude in decimal degrees. "
                "Use together with --west, --north, --east or pass none of them."
            ),
        ),
    ] = None,
    west: Annotated[
        float | None,
        typer.Option(
            "--west",
            help="Bounding box west longitude in decimal degrees.",
        ),
    ] = None,
    north: Annotated[
        float | None,
        typer.Option(
            "--north",
            help="Bounding box north latitude in decimal degrees.",
        ),
    ] = None,
    east: Annotated[
        float | None,
        typer.Option(
            "--east",
            help="Bounding box east longitude in decimal degrees.",
        ),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Two-letter US state code (e.g. MT, OK)."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-n",
            help=(
                "Cap on the number of results (default 50). A no-filter browse of "
                "a large office can return thousands of rows; the cap keeps the "
                "response bounded. Pass `0` for no cap. When the cap kicks in the "
                "response carries `truncated: true`, `total_count`, and a "
                "`truncation_hint`."
            ),
        ),
    ] = places.DEFAULT_BROWSE_LIMIT,
    cursor: Annotated[
        str | None,
        typer.Option(
            "--cursor",
            help=(
                "Opaque pagination cursor from a prior call's `next_cursor`. "
                "Pass it back to fetch the next page; omit for the first page. "
                "A stale cursor returns the `invalid_cursor` error (exit 2) — "
                "re-run without --cursor to restart."
            ),
        ),
    ] = None,
) -> None:
    """Browse the locations published by one office, with optional filters.

    All four bounding-box corners must be set together or none. When
    --state and a bbox are both set, both filters apply. Returns the same
    enriched per-place records as `place search` (including `parameters` and
    the `data_at` repair hint), with `result_count`, `ghost_count`, and
    `total_count` totals at the top.
    """
    if limit < 0:
        emit_error(
            CwmsToolsError.of(
                ErrorCode.USAGE_ERROR,
                "--limit must be a non-negative integer.",
                field="limit",
                offending_value=limit,
                hint="Pass --limit 0 for no cap, or any non-negative integer.",
            )
        )
    provided = [v for v in (south, west, north, east) if v is not None]
    if len(provided) not in {0, 4}:
        emit_error(
            CwmsToolsError.of(
                ErrorCode.USAGE_ERROR,
                "When specifying a bounding box, --south, --west, --north, "
                "--east must all be provided.",
                field="bbox",
                offending_value={"south": south, "west": west, "north": north, "east": east},
                hint="Pass all four bbox edges or omit bbox entirely.",
            )
        )

    bbox: BBox | None = None
    if south is not None and west is not None and north is not None and east is not None:
        bbox = BBox(south=south, west=west, north=north, east=east)

    try:
        emit(
            places.browse_region(
                office=office,
                bbox=bbox,
                state=state,
                limit=None if limit == 0 else limit,
                cursor=cursor,
            )
        )
    except CwmsToolsError as err:
        emit_error(err)
