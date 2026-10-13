"""`cwms-tools place ...` — place/location commands.

Subcommands:
- `cwms-tools place search <query> --office <O>`
- `cwms-tools place describe <office>/<name>`
- `cwms-tools place parameters <office>/<name>`
"""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.render import (
    attach_ghost_office_repair,
    attach_ghost_office_spec_repair,
    attach_repair_intent,
    emit_error,
    emit_model,
    localize_repair,
    rewrite_error_field,
)
from cwms_tools.core import places, shaping
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.models import (
    DescribePlaceResponse,
    Detail,
    ListParametersResponse,
    SearchPlacesResponse,
)

app = typer.Typer(
    name="place",
    help=(
        "Resolve and describe CWMS locations: name search, full place "
        "description (location + project + publishers + catalog last-data hint), and "
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
                value=spec,
                reason="Pass the place id as OFFICE/NAME, e.g. NWDM/FTPK.",
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
                "(e.g. `-o NWDP -o NWDM`). If unknown, omit it: on a fresh "
                "cache the first call returns no rows and a `repair_hint`; "
                "keep following `repair_hint` while one is present (optional "
                "once its `next_step` is `match_found_offices_skipped`; the chain "
                "ends when a response carries none; see `scope_note`, not proof "
                "of absence). If you know the "
                "region, pass the office directly (NWDM Missouri mainstem, "
                "NWDP Columbia/Snake, SWT Oklahoma, MVS St. Louis)."
            ),
        ),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option(
            "--state",
            help=(
                "Two-letter state code (e.g. CA, PR): search the districts whose "
                "area covers it (coverage under `guidance.coverage` in "
                "`cwms-tools offices`). Not with --office. Only selects offices; "
                "rows may lie outside the state."
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
                "what was filtered out. If every match is dropped and no "
                "office catalog read publishes the code, the response may add "
                "`parameter_suggestions` (similar published codes)."
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
                "carries `has_more: true`, `next_cursor`, and `total_count` "
                "(fully pageable — `truncated` stays false)."
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

    A top-level `repair_hint` (with `partial: true`) keeps the search
    followable to exhaustion; it appears in three cases. When `--office` is
    omitted and the resolved scope is empty, it names the curated
    data-bearing offices to retry with. When the per-call fan-out budget
    skips offices, it names the skipped ones (followed, when `--office` was
    omitted, by the curated offices not yet searched). When `--office` is omitted,
    only some curated offices are cached, and no match has data (barren
    records do not count), it names the curated offices that were never
    searched. Once a match with data is found, a budget skip carries `next_step`
    `match_found_offices_skipped` instead: same continuation, but optional;
    stop if a result answers the query. Keep following any other
    `repair_hint`; the chain ends when a response carries none. A response with no match that has
    data and no `repair_hint` carries `scope_note`: only `offices_searched`
    were searched, so this is not proof the place is absent from CWMS; keep
    earlier hops' results, and pass `--office` explicitly to widen (codes
    and region hints: `cwms-tools offices`). Offices that failed with a
    temporary upstream error are listed in `partial_reasons` as
    `temporary_errors_not_retried` for the caller to retry explicitly.
    """
    if limit < 0:
        emit_error(
            CwmsToolsError.of(
                ErrorCode.USAGE_ERROR,
                "--limit must be a non-negative integer.",
                field="limit",
                value=limit,
                reason="Pass --limit 0 for no cap, or any non-negative integer.",
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
            state=state,
            parameter=parameter,
            limit=effective_limit,
            cursor=cursor,
        )
    except CwmsToolsError as err:
        repair_args: dict[str, object] = {"query": query}
        if state is not None:
            repair_args["state"] = state
        if parameter is not None:
            repair_args["parameter"] = parameter
        repair_args["limit"] = limit
        if cursor is not None:
            repair_args["cursor"] = cursor
        repair_args["detail"] = detail.value
        # Repair targets the actual CLI invocation, not the MCP tool (#69):
        # `cwms_search_places` isn't a runnable command, so mechanical retry
        # must name `cwms-tools place search`, mirroring the sibling
        # describe/parameters/value commands.
        attach_ghost_office_repair(err, tool="cwms-tools place search", args=repair_args)
        attach_repair_intent(
            err,
            tool="cwms-tools place search",
            args={**repair_args, **({"office": list(office)} if office else {})},
        )
        emit_error(localize_repair(err))
    emit_model(SearchPlacesResponse, shaping.shape_place_detail(payload, detail))


@app.command("describe")
def describe(
    spec: Annotated[
        str,
        typer.Argument(
            help=(
                "OFFICE/NAME with the case-sensitive catalog id (from `place search`), "
                "e.g. SWT/FOSS (not SWT/Foss)."
            )
        ),
    ],
    detail: Annotated[
        Detail,
        typer.Option(
            "--detail",
            help=(
                "'summary' returns the triage subset of the location DTO and trims "
                f"`project` to {', '.join(shaping.PROJECT_SUMMARY_KEYS)} (dropping its "
                "nested location, costs, and prose); 'full' returns every field."
            ),
        ),
    ] = Detail.SUMMARY,
    include_locks: Annotated[
        bool,
        typer.Option(
            "--include-locks",
            help=(
                "Add navigation-lock dimensions (width, length, lift, draft) for a "
                "PROJECT or LOCK location. A PROJECT adds one list call plus one call "
                "per lock; a LOCK adds one call. A failed lock lookup sets `partial` "
                "instead of failing the command."
            ),
        ),
    ] = False,
) -> None:
    """Print everything about one place in a single call.

    Combines the location record, project metadata (when present), the
    parameters published at the location grouped by publisher, and the
    most recent data timestamp. Sets `partial: true` when any
    underlying lookup degrades.
    """
    office, name = _parse_office_slash_name(spec)
    try:
        payload = places.describe_place(office, name, include_locks=include_locks)
    except CwmsToolsError as err:
        # No `--office` flag on this command — `spec` is the retryable arg.
        # Repair targets the actual CLI invocation (#69 review): `spec` isn't
        # a real MCP `cwms_describe_place` argument, so the repair.tool must
        # be the CLI command, not the MCP tool name.
        rewrite_error_field(err, when="office_id", to="spec")
        # Location lookup failures name `name` (the MCP arg); here it's in `spec`.
        rewrite_error_field(err, when="name", to="spec")
        attach_ghost_office_spec_repair(
            err,
            tool="cwms-tools place describe",
            spec_key="spec",
            spec_suffix=name,
            args={
                **({"include_locks": True} if include_locks else {}),
                "detail": detail.value,
            },
        )
        attach_repair_intent(
            err,
            tool="cwms-tools place describe",
            args={
                **({"include_locks": True} if include_locks else {}),
                "detail": detail.value,
            },
            spec_key="spec",
            spec_fields={"office": office, "name": name},
        )
        emit_error(localize_repair(err))
    shaped = shaping.shape_place_detail(payload, detail)
    # The CLI has no `source` wrapper to fold these into (unlike MCP's
    # source.workaround/source.upstream_status); with no consumer, they were
    # pure noise leaking into every response — drop them for parity (#74).
    shaped.pop("source_workaround", None)
    shaped.pop("upstream_status", None)
    emit_model(DescribePlaceResponse, shaped)


@app.command("parameters")
def parameters(
    spec: Annotated[
        str,
        typer.Argument(
            help=(
                "OFFICE/NAME with the case-sensitive catalog id (from `place search`), "
                "e.g. SWT/FOSS (not SWT/Foss)."
            )
        ),
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
        # No `--office` flag on this command — `spec` is the retryable arg.
        # Repair targets the CLI invocation (#69 review): see cwms_describe_place.
        rewrite_error_field(err, when="office_id", to="spec")
        rewrite_error_field(err, when="name", to="spec")
        attach_ghost_office_spec_repair(
            err, tool="cwms-tools place parameters", spec_key="spec", spec_suffix=name, args={}
        )
        attach_repair_intent(
            err,
            tool="cwms-tools place parameters",
            args={},
            spec_key="spec",
            spec_fields={"office": office, "name": name},
        )
        emit_error(localize_repair(err))
    # No `--detail` toggle here; routed through the shared shaper (a no-op for
    # this response shape) to stay structurally in lockstep with the
    # `cwms_list_parameters` MCP tool, which applies the same place shaper.
    emit_model(ListParametersResponse, shaping.shape_place_detail(payload, Detail.SUMMARY))
