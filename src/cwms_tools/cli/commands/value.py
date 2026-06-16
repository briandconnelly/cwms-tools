"""`cwms-tools value get | history` — value tools."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

import typer

from cwms_tools.cli.exit_codes import from_error_code
from cwms_tools.cli.render import emit, emit_error
from cwms_tools.core import values
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.models import Detail, Rollup, Unit

app = typer.Typer(
    name="value",
    help=(
        "Read CWMS observations: the latest value (fast value-only by default) "
        "and a windowed history. Use `--with-status` on `get` to classify "
        "against applicable thresholds."
    ),
    no_args_is_help=True,
)


def _parse_id(spec: str) -> tuple[str, str, str]:
    """Parse `OFFICE/NAME/PARAMETER`, e.g. `NWDM/FTPK/Elev`."""
    parts = spec.split("/", 2)
    if len(parts) != 3 or any(not p.strip() for p in parts):
        emit_error(
            CwmsToolsError.of(
                ErrorCode.USAGE_ERROR,
                "Expected `OFFICE/NAME/PARAMETER` form, e.g. `NWDM/FTPK/Elev`.",
                field="id",
                offending_value=spec,
                hint="Pass each id as OFFICE/NAME/PARAMETER, e.g. NWDM/FTPK/Elev.",
            )
        )
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


@app.command("get")
def get(
    id_specs: Annotated[
        list[str],
        typer.Argument(
            help=(
                "One or more place/parameter ids, each in OFFICE/NAME/PARAMETER "
                "form. Examples: `NWDM/FTPK/Elev`, "
                "`NWDP/UBLW_S1-D21,0ft/Temp-Water` (depth-tagged sensor with "
                "comma in the name)."
            )
        ),
    ],
    window_hours: Annotated[
        int,
        typer.Option(
            "--window-hours",
            help="How far back to search for the most recent value, in hours.",
        ),
    ] = 24,
    unit: Annotated[
        Unit,
        typer.Option(
            "--unit",
            help="Unit system: 'EN' (English: ft, cfs) or 'SI' (metric: m, cms).",
        ),
    ] = Unit.EN,
    with_status: Annotated[
        bool,
        typer.Option(
            "--with-status/--no-status",
            help=(
                "Classify the observation against the applicable CWMS Location "
                "Levels. OFF by default — the levels lookup is reliably slow "
                "(often exceeds the 8 s budget). When ON the response carries "
                "`status_class` plus `level_lookup_status` indicating whether "
                "the lookup ran to completion, timed out, or returned no "
                "thresholds."
            ),
        ),
    ] = False,
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help="'summary' drops chatty per-threshold internals; 'full' keeps them.",
        ),
    ] = Detail.SUMMARY,
) -> None:
    """Get the latest observation for one or more place/parameters.

    Default path is value-only and fast. Pass `--with-status` to also
    classify against applicable thresholds (slower; the response always
    carries `level_lookup_status` so you can see what happened).

    With multiple ids the response is a batch envelope: per-item results
    land inline, `partial: true` is set when any item failed, and the
    process exits non-zero on partial failure.

    Example: `cwms-tools value get NWDP/UBLW_S1-D21,0ft/Temp-Water --unit SI`
    """
    results: list[dict] = []
    ok_count = 0
    failed_count = 0
    last_exit_code = 0
    for spec in id_specs:
        # A malformed id is a whole-command usage error: `_parse_id` emits the
        # envelope to stderr and exits before any aggregate is written.
        office, name, parameter = _parse_id(spec)
        try:
            payload = values.get_value(
                office,
                name,
                parameter,
                window=timedelta(hours=window_hours),
                unit=unit.value,
                classify_against_levels=with_status,
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
        typer.Argument(
            help=(
                "Place/parameter id in OFFICE/NAME/PARAMETER form. Examples: "
                "`SWT/FOSS/Elev`, `NWDP/UBLW_S1-D21,0ft/Temp-Water` "
                "(depth-tagged WQ sensor)."
            )
        ),
    ],
    begin: Annotated[
        str,
        typer.Option(
            "--begin",
            help="Window start as an RFC3339 timestamp (e.g. 2026-05-17T00:00:00Z).",
        ),
    ],
    end: Annotated[
        str,
        typer.Option(
            "--end",
            help="Window end as an RFC3339 timestamp (e.g. 2026-05-18T00:00:00Z).",
        ),
    ],
    unit: Annotated[
        Unit,
        typer.Option(
            "--unit",
            help="Unit system: 'EN' (English: ft, cfs) or 'SI' (metric: m, cms).",
        ),
    ] = Unit.EN,
    rollup: Annotated[
        Rollup,
        typer.Option(
            "--rollup",
            help=(
                "'raw' returns every point; 'hourly'/'daily' return per-bucket "
                "min/max/mean/count (UTC buckets) for compact trends. The `summary` "
                "key is always present regardless of rollup (null only when the "
                "window has no numeric observations)."
            ),
        ),
    ] = Rollup.RAW,
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help="'summary' omits per-point quality codes; 'full' includes them.",
        ),
    ] = Detail.SUMMARY,
) -> None:
    """Read a windowed history of one parameter at one place.

    Sets `truncated: true` with a `truncation_hint` when the upstream
    page cap (300,000 points) clipped the requested window. For trend
    questions, read the always-present `summary` block or pass
    `--rollup hourly|daily` for compact per-bucket aggregates.
    """
    office, name, parameter = _parse_id(id_spec)
    begin_dt = _parse_iso(begin, field="begin")
    end_dt = _parse_iso(end, field="end")
    try:
        payload = values.get_history(
            office,
            name,
            parameter,
            begin=begin_dt,
            end=end_dt,
            unit=unit.value,
            rollup=rollup.value,
        )
        if detail is Detail.SUMMARY and isinstance(payload.get("values"), list):
            payload = {
                **payload,
                "values": [
                    {k: v for k, v in row.items() if k != "quality"} for row in payload["values"]
                ],
            }
        emit(payload)
    except CwmsToolsError as err:
        emit_error(err)


def _parse_iso(value: str, *, field: str) -> datetime:
    """Parse an RFC3339 timestamp or emit a precise INVALID_FIELD error to stderr."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        emit_error(
            CwmsToolsError.of(
                ErrorCode.INVALID_FIELD,
                f"Could not parse --{field} as RFC3339: {exc}",
                field=field,
                offending_value=value,
                hint="RFC3339 with timezone, e.g. 2026-05-17T00:00:00Z",
            )
        )
