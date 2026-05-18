"""`cwms-tools value get | history` — value tools."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

import typer

from cwms_tools.cli.exit_codes import from_error_code
from cwms_tools.cli.render import emit
from cwms_tools.core import values
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.models import Detail

app = typer.Typer(
    name="value",
    help="Current value (with status context) and windowed history at a CWMS location.",
    no_args_is_help=True,
)


def _parse_id(spec: str) -> tuple[str, str, str]:
    """Parse `OFFICE/NAME/PARAMETER`, e.g. `NWDM/FTPK/Elev`."""
    parts = spec.split("/", 2)
    if len(parts) != 3 or any(not p.strip() for p in parts):
        emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.USAGE_ERROR.value,
                    "message": "Expected `OFFICE/NAME/PARAMETER` form, e.g. `NWDM/FTPK/Elev`.",
                    "field": "id",
                    "offending_value": spec,
                },
            }
        )
        raise typer.Exit(code=2)
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


@app.command("get")
def get(
    id_specs: Annotated[
        list[str],
        typer.Argument(help="One or more `OFFICE/NAME/PARAMETER` ids."),
    ],
    window_hours: Annotated[
        int,
        typer.Option("--window-hours", help="How far back to search for the most recent value."),
    ] = 24,
    unit: Annotated[
        str,
        typer.Option("--unit", help="Unit system: EN or SI."),
    ] = "EN",
    detail: Annotated[
        Detail,
        typer.Option("--detail", help="Response density (summary or full)."),
    ] = Detail.SUMMARY,
) -> None:
    """Latest value with inline status classification.

    Accepts multiple ids. Per-item errors land inline; the process exits
    non-zero on any failure (§"Batch / multi-item semantics" of the plan).
    """
    results: list[dict] = []
    ok_count = 0
    failed_count = 0
    last_exit_code = 0
    for spec in id_specs:
        try:
            office, name, parameter = _parse_id(spec)
        except typer.Exit as ex:
            # _parse_id already emitted an error; re-raise on first bad shape.
            raise ex
        try:
            payload = values.get_value(
                office,
                name,
                parameter,
                window=timedelta(hours=window_hours),
                unit=unit,
            )
            if detail is Detail.SUMMARY and isinstance(payload.get("thresholds_active"), list):
                payload = {
                    **payload,
                    "thresholds_active": [
                        {k: v for k, v in t.items() if k not in {"level_id", "source_workaround"}}
                        for t in payload["thresholds_active"]
                    ],
                }
            results.append({"id": spec, "ok": True, "data": payload})
            ok_count += 1
        except CwmsToolsError as err:
            results.append({"id": spec, "ok": False, "error": err.envelope.model_dump(mode="json")})
            failed_count += 1
            last_exit_code = from_error_code(err.envelope.code)

    partial = failed_count > 0
    output = {
        "partial": partial,
        "summary": {"requested": len(id_specs), "ok": ok_count, "failed": failed_count},
        "results": results,
    }
    emit(output)
    if partial:
        raise typer.Exit(code=last_exit_code or 1)


@app.command("history")
def history(
    id_spec: Annotated[
        str,
        typer.Argument(help="`OFFICE/NAME/PARAMETER` id, e.g. `SWT/FOSS/Elev`."),
    ],
    begin: Annotated[
        str,
        typer.Option("--begin", help="Window start, RFC3339 (e.g. 2026-05-17T00:00:00Z)."),
    ],
    end: Annotated[
        str,
        typer.Option("--end", help="Window end, RFC3339."),
    ],
    unit: Annotated[
        str,
        typer.Option("--unit", help="Unit system: EN or SI."),
    ] = "EN",
    detail: Annotated[
        Detail,
        typer.Option("--detail", help="Response density (summary or full)."),
    ] = Detail.SUMMARY,
) -> None:
    """Windowed history for one parameter at one location (§9.2)."""
    office, name, parameter = _parse_id(id_spec)
    try:
        begin_dt = datetime.fromisoformat(begin.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.INVALID_FIELD.value,
                    "message": f"Could not parse begin/end as RFC3339: {exc}",
                    "field": "begin/end",
                },
            }
        )
        raise typer.Exit(code=2) from exc
    try:
        payload = values.get_history(office, name, parameter, begin=begin_dt, end=end_dt, unit=unit)
        if detail is Detail.SUMMARY and isinstance(payload.get("values"), list):
            payload = {
                **payload,
                "values": [
                    {k: v for k, v in row.items() if k != "quality"} for row in payload["values"]
                ],
            }
        emit(payload)
    except CwmsToolsError as err:
        emit({"ok": False, "error": err.envelope.model_dump(mode="json")})
        raise typer.Exit(code=from_error_code(err.envelope.code)) from err
