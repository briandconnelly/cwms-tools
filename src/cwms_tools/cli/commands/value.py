"""`cwms-tools value get | history` — value tools."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

import typer

from cwms_tools.cli.exit_codes import from_error_code
from cwms_tools.cli.render import (
    attach_ghost_office_spec_repair,
    emit,
    emit_error,
    rewrite_error_field,
)
from cwms_tools.core import shaping, values
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint
from cwms_tools.core.models import Detail, Rollup, Unit
from cwms_tools.core.offices import nw_rollup_target

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
            payload = shaping.shape_value_detail(payload, detail)
            results.append({"id": spec, "ok": True, "data": payload})
            ok_count += 1
        except CwmsToolsError as err:
            # #68: per-item errors here bypass `emit_error()` (this is the
            # success-shaped batch envelope, not a whole-command failure). No
            # `--office` flag on this command — `id_specs` (the declared CLI
            # argument, per `cli/commands/schema.py`) is the retryable arg.
            rewrite_error_field(err, when="office_id", to="id_specs")
            # #69: repair retries just THIS failed spec (not the whole
            # original id_specs list, which would re-run already-ok items),
            # office rolled up into a fresh OFFICE/NAME/PARAMETER spec.
            # Targets the CLI invocation, not the MCP tool (#69 review):
            # `id_specs` isn't a real `cwms_get_value` MCP argument.
            office_id = err.envelope.offending_value
            if err.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
                target = nw_rollup_target(office_id)
                err.envelope.repair = RepairHint(
                    tool="cwms-tools value get",
                    args={
                        "id_specs": [f"{target}/{name}/{parameter}"],
                        "window_hours": window_hours,
                        "unit": unit.value,
                        "with_status": with_status,
                        "detail": detail.value,
                    },
                )
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
                "'raw' returns every point, up to a server-side cap (currently "
                "5,000 — see `truncated`); 'hourly'/'daily' return per-bucket "
                "min/max/mean/count (UTC buckets) for compact trends and are not "
                "subject to that cap. The `summary` key is always present "
                "regardless of rollup (null only when the window has no numeric "
                "observations)."
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

    Sets `truncated: true` with a `truncation_hint` when either the raw-point
    response cap (5,000 points under `--rollup raw`) trims `values`, or the
    upstream page cap (300,000 points) clipped the fetch itself before
    reaching the requested window end — in that case `summary`/`buckets`
    cover only the fetched prefix, not the full window, and switching
    `--rollup` doesn't recover the rest; continue via `next_begin` and
    repeat until `truncated` is false. Otherwise, for trend questions, read
    the always-present `summary` block or pass `--rollup hourly|daily` for a
    compact per-bucket summary of the full window in one call.
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
        emit(shaping.shape_history_detail(payload, detail))
    except CwmsToolsError as err:
        # No `--office` flag on this command — `id_spec` (the declared CLI
        # argument, per `cli/commands/schema.py`) is the retryable arg.
        # Repair targets the CLI invocation, not the MCP tool (#69 review):
        # `id_spec`/`begin`/`end` aren't real `cwms_get_history` MCP args
        # (that tool takes separate office/name/begin_iso/end_iso).
        rewrite_error_field(err, when="office_id", to="id_spec")
        attach_ghost_office_spec_repair(
            err,
            tool="cwms-tools value history",
            spec_key="id_spec",
            spec_suffix=f"{name}/{parameter}",
            args={
                "begin": begin,
                "end": end,
                "unit": unit.value,
                "rollup": rollup.value,
                "detail": detail.value,
            },
        )
        emit_error(err)


@app.command("profile")
def profile(
    id_spec: Annotated[
        str,
        typer.Argument(
            help=(
                "Parent string + parameter in OFFICE/NAME/PARAMETER form, where "
                "NAME is the parent 'string' (e.g. `NWDP/GWLW_S1/Temp-Water`), "
                "NOT a single depth-tagged sensor."
            )
        ),
    ],
    window_hours: Annotated[
        int,
        typer.Option(
            "--window-hours",
            help="How far back to search for each sensor's most recent value, in hours.",
        ),
    ] = 24,
    unit: Annotated[
        Unit,
        typer.Option(
            "--unit",
            help="Unit system: 'EN' (ft, °F) or 'SI' (m, °C).",
        ),
    ] = Unit.EN,
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help="'summary' drops the per-sensor ts_id; 'full' keeps it.",
        ),
    ] = Detail.SUMMARY,
) -> None:
    """Read every depth sensor of one string in a single call.

    Returns the sensors sorted shallow→deep, each with structured
    `depth: {value, unit}` and its latest value — a one-shot vertical
    profile instead of one `value get` per depth.

    Example: `cwms-tools value profile NWDP/GWLW_S1/Temp-Water`
    """
    office, name, parameter = _parse_id(id_spec)
    try:
        payload = values.get_profile(
            office, name, parameter, window=timedelta(hours=window_hours), unit=unit.value
        )
        emit(shaping.shape_profile_detail(payload, detail))
    except CwmsToolsError as err:
        # No `--office` flag on this command — `id_spec` (this command's
        # actual positional argument, matching `history`'s) is the retryable
        # arg. NOTE: `value profile` itself is missing from the machine
        # schema in `cli/commands/schema.py` (#84, found during #68 review).
        # Repair targets the CLI invocation, not the MCP tool (#69 review).
        rewrite_error_field(err, when="office_id", to="id_spec")
        attach_ghost_office_spec_repair(
            err,
            tool="cwms-tools value profile",
            spec_key="id_spec",
            spec_suffix=f"{name}/{parameter}",
            args={"window_hours": window_hours, "unit": unit.value, "detail": detail.value},
        )
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
