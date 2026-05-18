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
    help="Resolve, describe, and inspect CWMS locations.",
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
    query: Annotated[str, typer.Argument(help="Name fragment to match (case-insensitive).")],
    office: Annotated[
        str,
        typer.Option("--office", "-o", help="USACE office code (e.g. NWDM, SWT, MVS)."),
    ],
    detail: Annotated[
        Detail,
        typer.Option("--detail", help="Response density (summary or full)."),
    ] = Detail.SUMMARY,
) -> None:
    """Search a place by name within an office (§9.1 steps 1-2)."""
    try:
        payload = places.search_places(query, office=office)
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
        typer.Argument(help="OFFICE/NAME form, e.g. `NWDM/FTPK` or `SWT/FOSS`."),
    ],
    detail: Annotated[
        Detail,
        typer.Option("--detail", help="Response density (summary or full)."),
    ] = Detail.SUMMARY,
) -> None:
    """Describe a place — full Location + Project + publisher fingerprint (§9.9)."""
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
        typer.Argument(help="OFFICE/NAME form, e.g. `SWT/FOSS`."),
    ],
) -> None:
    """List parameters at a location, grouped by publisher (§9.6)."""
    office, name = _parse_office_slash_name(spec)
    try:
        emit(places.list_parameters(office, name))
    except CwmsToolsError as err:
        emit({"ok": False, "error": err.envelope.model_dump(mode="json")})
        raise typer.Exit(code=from_error_code(err.envelope.code)) from err
