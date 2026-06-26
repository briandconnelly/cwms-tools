"""`cwms-tools place ...` — place/location commands.

Subcommands:
- `cwms-tools place search <query> --office <O>`
- `cwms-tools place describe <office>/<name>`
- `cwms-tools place parameters <office>/<name>`
"""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.render import emit, emit_error
from cwms_tools.core import places, shaping
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.models import Detail

app = typer.Typer(
    name="place",
    help=(
        "Resolve and describe CWMS locations: name search, full place "
        "description (location + project + publishers + freshness), and "
        "per-location parameter listing."
    ),
    no_args_is_help=True,
)


def _parse_office_slash_name(spec: str) -> tuple[str, str]:
    """Split `OFFICE/NAME` into (office, name); raises typer.Exit(2) on bad shape."""
    if "/" not in spec:
        emit_error(
            CwmsToolsError.of(
                ErrorCode.USAGE_ERROR,
                "Expected `OFFICE/NAME` form, e.g. `NWDM/FTPK`.",
                field="spec",
                offending_value=spec,
                hint="Pass the place id as OFFICE/NAME, e.g. NWDM/FTPK.",
            )
        )
    office, name = spec.split("/", 1)
    return office.strip(), name.strip()


@app.command("search")
def search(
    query: Annotated[
        str,
        typer.Argument(help="Name fragment to match, case-insensitive."),
    ],
    office: Annotated[
        list[str] | None,
        typer.Option(
            "--office",
            "-o",
            help=(
                "USACE office code (list valid codes with `cwms-tools "
                "offices`). Repeat to fan out across multiple offices "
                "(e.g. `-o NWDP -o NWDM`). Omit to use offices already "
                "cached this session; unbounded discovery is intentionally "
                "avoided. Overflow beyond the per-call budget lands in "
                "`offices_skipped_for_budget`. When an omitted `--office` "
                "resolves to an empty scope, the response carries a "
                "top-level `repair_hint` naming a concrete data-bearing "
                "office list to retry with."
            ),
        ),
    ] = None,
    parameter: Annotated[
        str | None,
        typer.Option(
            "--parameter",
            "-p",
            help=(
                "Filter to locations publishing this parameter "
                "(e.g. Temp-Water, Elev, Flow-In). When set, non-publishing "
                "rows are dropped — except barren parents whose `data_at` "
                "siblings publish it. `nearby_non_matching_count` reflects "
                "what was filtered out."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-n",
            help=(
                "Cap on the number of results (default 50). Broad searches "
                "like 'Temp String' on a big office can match hundreds of "
                "rows; the cap keeps responses small. Pass `0` to return "
                "every match (no cap). When the cap kicks in the response "
                "carries `truncated: true` and `total_count`."
            ),
        ),
    ] = places.DEFAULT_SEARCH_LIMIT,
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
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help="Response density. 'summary' drops verbose upstream fields; 'full' keeps them.",
        ),
    ] = Detail.SUMMARY,
) -> None:
    """Search for places by name across one or more offices in scope.

    Each result is enriched with parameter_count (0 = ghost record),
    active publishers, last data timestamp, co-located variants, and
    `data_at` — when a barren parent has a co-located sibling that
    publishes data (e.g. the Lake Washington `UBLW_S1` parent has no
    ts ids but `UBLW_S1-D21,0ft` does), `data_at` names that sibling
    so the agent doesn't have to walk the co_located list to find it.
    Data-bearing records sort first.

    Depth-tagged WQ sensor rows (e.g. `GWLW_S1-D3,0ft`) carry a
    structured `depth: {value, unit}` (e.g. `{value: 3.0, unit: "ft"}`),
    so there's no need to parse the cryptic id.

    When `--office` is omitted and the resolved scope is empty, the
    response carries a top-level `repair_hint` naming a concrete
    data-bearing office list to retry with.
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
    effective_limit = None if limit == 0 else limit
    # Typer passes repeatable Options as a list[str] (even when one value
    # was given). Collapse to a single string when only one office is
    # present, so the response's `office` field echoes the simpler shape.
    office_arg: str | list[str] | None
    if not office:
        office_arg = None
    elif len(office) == 1:
        office_arg = office[0]
    else:
        office_arg = list(office)
    try:
        payload = places.search_places(
            query,
            office=office_arg,
            parameter=parameter,
            limit=effective_limit,
            cursor=cursor,
        )
    except CwmsToolsError as err:
        emit_error(err)
    emit(shaping.shape_place_detail(payload, detail))


@app.command("describe")
def describe(
    spec: Annotated[
        str,
        typer.Argument(help="Place id in OFFICE/NAME form, e.g. NWDM/FTPK or SWT/FOSS."),
    ],
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help=(
                "'summary' returns the triage subset of the location DTO; "
                "'full' returns every field."
            ),
        ),
    ] = Detail.SUMMARY,
) -> None:
    """Print everything about one place in a single call.

    Combines the location record, project metadata (when present), the
    parameters published at the location grouped by publisher, and the
    most recent data timestamp. Sets `partial: true` when any
    underlying lookup degrades.
    """
    office, name = _parse_office_slash_name(spec)
    try:
        payload = places.describe_place(office, name)
    except CwmsToolsError as err:
        emit_error(err)
    emit(shaping.shape_place_detail(payload, detail))


@app.command("parameters")
def parameters(
    spec: Annotated[
        str,
        typer.Argument(help="Place id in OFFICE/NAME form, e.g. SWT/FOSS."),
    ],
) -> None:
    """List the parameters published at a place, grouped by publisher.

    The cheapest probe for distinguishing data-bearing locations from
    ghost catalog records: a ghost returns ts_count=0 and an empty
    by_publisher list.
    """
    office, name = _parse_office_slash_name(spec)
    try:
        payload = places.list_parameters(office, name)
    except CwmsToolsError as err:
        emit_error(err)
    # No `--detail` toggle here; routed through the shared shaper (a no-op for
    # this response shape) to stay structurally in lockstep with the
    # `cwms_list_parameters` MCP tool, which applies the same place shaper.
    emit(shaping.shape_place_detail(payload, Detail.SUMMARY))
