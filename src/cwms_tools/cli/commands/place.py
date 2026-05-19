"""`cwms-tools place ...` — place/location commands.

Subcommands:
- `cwms-tools place search <query> --office <O>`
- `cwms-tools place describe <office>/<name>`
- `cwms-tools place parameters <office>/<name>`
"""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.exit_codes import from_error_code
from cwms_tools.cli.render import emit
from cwms_tools.core import places
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
        emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.USAGE_ERROR.value,
                    "message": "Expected `OFFICE/NAME` form, e.g. `NWDM/FTPK`.",
                    "field": "spec",
                    "offending_value": spec,
                },
            }
        )
        raise typer.Exit(code=2)
    office, name = spec.split("/", 1)
    return office.strip(), name.strip()


@app.command("search")
def search(
    query: Annotated[
        str,
        typer.Argument(help="Name fragment to match, case-insensitive."),
    ],
    office: Annotated[
        str,
        typer.Option(
            "--office",
            "-o",
            help=(
                "USACE office code (e.g. NWDM, SWT, MVS). Required because "
                "catalog search is per-office."
            ),
        ),
    ],
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
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help="Response density. 'summary' drops verbose upstream fields; 'full' keeps them.",
        ),
    ] = Detail.SUMMARY,
) -> None:
    """Search for places by name in one office.

    Each result is enriched with parameter_count (0 = ghost record),
    active publishers, last data timestamp, co-located variants, and
    `data_at` — when a barren parent has a co-located sibling that
    publishes data (e.g. the Lake Washington `UBLW_S1` parent has no
    ts ids but `UBLW_S1-D21,0ft` does), `data_at` names that sibling
    so the agent doesn't have to walk the co_located list to find it.
    Data-bearing records sort first.
    """
    if limit < 0:
        emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.USAGE_ERROR.value,
                    "message": "--limit must be a non-negative integer.",
                    "field": "limit",
                    "offending_value": limit,
                },
            }
        )
        raise typer.Exit(code=2)
    effective_limit = None if limit == 0 else limit
    try:
        payload = places.search_places(query, office=office, limit=effective_limit)
    except CwmsToolsError as err:
        emit({"ok": False, "error": err.envelope.model_dump(mode="json")})
        raise typer.Exit(code=from_error_code(err.envelope.code)) from err
    if detail is Detail.SUMMARY:
        payload = {
            **payload,
            "results": [{k: v for k, v in r.items() if k != "raw"} for r in payload["results"]],
        }
    emit(payload)


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
        emit({"ok": False, "error": err.envelope.model_dump(mode="json")})
        raise typer.Exit(code=from_error_code(err.envelope.code)) from err
    if detail is Detail.SUMMARY and isinstance(payload.get("location"), dict):
        loc = payload["location"]
        payload = {
            **payload,
            "location": {
                k: loc.get(k)
                for k in (
                    "office-id",
                    "name",
                    "location-kind",
                    "latitude",
                    "longitude",
                    "public-name",
                    "long-name",
                    "horizontal-datum",
                    "state-initial",
                    "nearest-city",
                    "timezone-name",
                )
                if k in loc
            },
        }
    emit(payload)


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
        emit(places.list_parameters(office, name))
    except CwmsToolsError as err:
        emit({"ok": False, "error": err.envelope.model_dump(mode="json")})
        raise typer.Exit(code=from_error_code(err.envelope.code)) from err
