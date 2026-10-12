"""Task-completing logic for the four place tools (M4).

This module is the layer between the MCP/CLI adapters and the per-resource
wrappers in `core.catalog`, `core.locations`, `core.projects`, and
`core.publishers`. It produces the response shapes declared in
`core.models` — task-response models, not raw upstream DTOs.
"""

from __future__ import annotations

from typing import Any

from cwms_tools.core import (
    catalog,
    concurrency,
    depth,
    locations,
    locks,
    offices,
    pagination,
    projects,
    publishers,
)
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.geo import BBox, GeoPoint, filter_by_bbox

DEFAULT_SEARCH_LIMIT: int = 50
DEFAULT_BROWSE_LIMIT: int = 50


def _fanout_budget() -> int:
    """How many uncached offices we will fetch per `search_places` call.

    Delegates to the shared `concurrency.fanout_budget()` so this and
    `publishers_for_parameter` enforce one identical cap; kept as a thin
    module-local seam that tests monkeypatch.
    """
    return concurrency.fanout_budget()


def _normalize_office_arg(office: str | list[str] | None) -> list[str] | None:
    """Return the explicit office list, or None to mean "use cached scope"."""
    if office is None:
        return None
    if isinstance(office, str):
        return [office]
    seen: set[str] = set()
    out: list[str] = []
    for o in office:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def search_places(
    query: str,
    *,
    office: str | list[str] | None = None,
    parameter: str | None = None,
    limit: int | None = DEFAULT_SEARCH_LIMIT,
    cursor: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_search_places` — name resolution with enrichment.

    Returns the SearchPlacesResult shape: ghost-filtered + co-located, ranked
    so data-bearing records come first. Each barren result carries a
    `data_at` repair hint listing co-located siblings that DO publish data
    (the depth-tagged child case from Lake Washington / UBLW_S1 — the parent
    is empty but `UBLW_S1-D21,0ft` holds the actual sensors).

    `office`: a single office code, an explicit list of office codes, or
    `None` to use the cached scope. `None` fans out only across offices
    whose unfiltered locations catalog is already cached this session;
    unbounded discovery is intentionally avoided. Pass an explicit list to
    widen. New (uncached) offices in the list are capped per call by
    `_fanout_budget()`; offices not fetched are listed under
    `offices_skipped_for_budget` — re-call with those offices in `office`
    to widen. This is the signal for *scope* incompleteness (offices never
    searched at all) and is orthogonal to `truncated`/`has_more` (which
    describe *row* completeness within the offices that WERE searched):
    `truncated: false` never implies every requested office was searched —
    check `offices_skipped_for_budget` for that. When the resolved scope is
    empty (a bare-name search with no cached offices), the response carries
    a top-level `repair_hint` (reason `no_offices_in_scope`): a ready-to-use
    retry call naming the curated data-bearing office list (priority order)
    under `repair_hint.args.office`. Separately, whenever any office was
    skipped for the fan-out budget (#161), the response sets `partial: true`
    and carries a `repair_hint` (reason `offices_skipped_for_budget`) whose
    `args.office` is exactly the skipped list; follow it repeatedly until
    `offices_skipped_for_budget` is empty. That hint carries no `cursor` or
    `limit` (a cursor locks the office set it was minted for) and is not
    attached on a cursor continuation.

    `parameter`: optional CWMS parameter code (e.g. `Temp-Water`). When
    set, data-bearing rows that do not publish this parameter are dropped
    from `results`; barren rows are kept only when their `data_at`
    siblings publish the parameter (the depth-sensor repair case). The
    response carries `nearby_non_matching_count` so the agent sees how
    much was filtered without paying for the filtered rows themselves.

    `limit` caps the number of results returned (default 50). Broad
    searches can return hundreds of rows on a big office; the cap
    prevents flooding the caller. Set `limit=None` (or `limit=0` on the
    CLI) to return every match.

    Pagination: when the result set exceeds `limit`, the response sets
    `has_more: true` and returns an opaque `next_cursor` — `total_count`
    reports the full size. Pass `next_cursor` back as `cursor` to fetch the
    next page; the cursor locks the searched office set and the
    query/parameter, so continuation is deterministic. `truncated` stays
    `false` here: unlike a genuine hard cap, every row beyond `limit` is
    still reachable via `next_cursor`, so it never means "unrecoverable"
    for this tool (#73). A stale cursor (changed query/parameter, or a
    catalog that shifted) raises an `invalid_cursor` error — restart
    without `cursor`. `limit=None` (or `limit=0` on the CLI) returns all
    results and never paginates; passing `cursor` together with an
    unlimited limit is rejected as `invalid_cursor`.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer or None")
    if limit == 0:
        limit = None  # 0 means "no cap"; normalize so pagination math is well-defined

    req = pagination.request_hash({"q": query, "parameter": parameter})
    decoded, offset, offices_searched, offices_skipped, partial_reasons = _resolve_scope(
        office=office, cursor=cursor, limit=limit, req=req, use_cache=use_cache
    )

    gathered, gather_reasons, temporary_errored = _gather_enriched(
        offices_searched,
        query,
        use_cache=use_cache,
        reraise_single=len(offices_searched) == 1 and not offices_skipped,
    )
    partial_reasons.extend(gather_reasons)
    enriched, filtered_out = _apply_parameter_filter(
        gathered,
        parameter,
        use_cache=use_cache,
    )
    enriched.sort(key=lambda r: (-r["parameter_count"], r["office_id"], r["name"]))
    total_count = len(enriched)
    if decoded is not None:
        pagination.ensure_total(decoded, total=total_count)  # catalog-shift guard

    next_cursor: str | None = None
    if limit is None:
        page = enriched[offset:]
        has_more = False
    else:
        next_offset = offset + limit
        page = enriched[offset:next_offset]
        has_more = next_offset < total_count
        if has_more:
            next_cursor = pagination.encode_cursor(
                {
                    "v": pagination.CURSOR_VERSION,
                    "kind": "search_places",
                    "off": next_offset,
                    "req": req,
                    "offices": offices_searched,
                    "total": total_count,
                }
            )

    results = [
        {
            "office_id": r["office_id"],
            "name": r["name"],
            "public_name": r.get("public_name"),
            "location_kind": r.get("location_kind"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "depth": depth.parse_depth(r["name"]),
            "parameter_count": r["parameter_count"],
            "parameters": r.get("parameters", []),
            "publishers": r["publishers"],
            "last_data_timestamp": r.get("last_data_timestamp"),
            "co_located": r.get("co_located", []),
            "data_at": r.get("data_at", []),
            # The unfiltered upstream location DTO (#74) — `shaping.shape_place_detail`
            # strips this in `summary` mode; only `detail=full` keeps it.
            "raw": r.get("raw"),
        }
        for r in page
    ]

    response: dict[str, Any] = {
        "query": query,
        "office": office,
        "offices_searched": offices_searched,
        "offices_skipped_for_budget": offices_skipped,
        "results": results,
        "total_count": total_count,
        # `limit` never makes rows unrecoverable — `next_cursor` can always
        # page through the rest — so `truncated` stays False here; `has_more`
        # is the pagination signal (#73).
        "truncated": False,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": limit,
    }
    if parameter is not None:
        response["parameter"] = parameter
        response["nearby_non_matching_count"] = filtered_out
    extra_reasons, repair_hint = _continuation(
        query,
        parameter,
        implicit_scope=office is None and decoded is None,
        offices_searched=offices_searched,
        offices_skipped=offices_skipped,
        temporary_errored=temporary_errored,
        total_count=total_count,
    )
    partial_reasons.extend(extra_reasons)
    if partial_reasons:
        response["partial"] = True
        response["partial_reasons"] = partial_reasons
    if repair_hint is not None:
        response["repair_hint"] = repair_hint
    return response


def _continuation(
    query: str,
    parameter: str | None,
    *,
    implicit_scope: bool,
    offices_searched: list[str],
    offices_skipped: list[str],
    temporary_errored: list[str],
    total_count: int,
) -> tuple[list[str], dict[str, Any] | None]:
    """Decide the continuation `partial_reasons` and `repair_hint` for a search.

    `implicit_scope` is true when the caller omitted `office` on a fresh search
    (no cursor). Hints apply to that case, and — for the budget hint — to any
    fresh search; an explicit empty list is a deliberate "search nothing" and is
    never widened. Precedence: empty scope (#24) > budget continuation (#161) >
    partly-warm cache. The budget hint restarts a fresh search (no cursor: a
    cursor locks the office set it was minted for) over the skipped offices; on an
    implicit scope the curated offices never searched ride along after them.
    Temporarily-errored offices are only reported (every hop), never re-carried:
    carrying them would let two persistently failing offices chain forever.
    """
    reasons: list[str] = []
    uncached = (
        [o for o in offices.discovery_office_candidates() if o not in offices_searched]
        if implicit_scope and offices_searched and total_count == 0
        else []
    )
    if temporary_errored:
        reasons.append(
            f"temporary_errors_not_retried: {', '.join(temporary_errored)} failed with a "
            "temporary upstream error and were not searched; retry them with `office`"
        )
    if uncached:
        reasons.append(
            f"cached_scope_incomplete: {len(uncached)} curated office(s) not cached and "
            "therefore not searched; follow `repair_hint`"
        )
    if implicit_scope and not offices_searched:
        return reasons, _empty_scope_repair_hint(query, parameter)
    if offices_skipped:  # always empty on the cursor path, which rejects skips outright
        return reasons, _skipped_scope_repair_hint(query, parameter, offices_skipped, uncached)
    if uncached:
        return reasons, _cached_scope_repair_hint(query, parameter, uncached)
    return reasons, None


def _skipped_scope_repair_hint(
    query: str, parameter: str | None, skipped: list[str], also: list[str]
) -> dict[str, Any]:
    """Build the `repair_hint` for a search that skipped offices for budget.

    `also` lists further offices to search after the skipped ones (deduplicated).
    """
    offices_next = list(dict.fromkeys([*skipped, *also]))
    args: dict[str, Any] = {"query": query, "office": offices_next}
    if parameter is not None:
        args["parameter"] = parameter
    return {
        "reason": "offices_skipped_for_budget",
        "message": (
            f"{len(skipped)} office(s) were not searched because of the per-call fan-out "
            "budget. Retry with `office` set to exactly this list to continue; repeat "
            "until `offices_skipped_for_budget` is empty."
        ),
        "tool": "cwms_search_places",
        "args": args,
    }


def _cached_scope_repair_hint(
    query: str, parameter: str | None, uncached: list[str]
) -> dict[str, Any]:
    """Build the `repair_hint` for a partly-warm cache that matched nothing."""
    args: dict[str, Any] = {"query": query, "office": list(uncached)}
    if parameter is not None:
        args["parameter"] = parameter
    return {
        "reason": "cached_scope_incomplete",
        "message": (
            "Only offices already cached were searched and nothing matched. Retry with "
            "`office` set to the curated offices that were not cached (priority order); "
            "keep following `repair_hint` until `offices_skipped_for_budget` is empty."
        ),
        "tool": "cwms_search_places",
        "args": args,
    }


def _empty_scope_repair_hint(query: str, parameter: str | None) -> dict[str, Any]:
    """Build the `repair_hint` for an empty-scope `cwms_search_places` result.

    Echoes the caller's `query`/`parameter` and names the curated data-bearing
    office set so the hint is copy-paste retryable as the next call's `office`.
    """
    args: dict[str, Any] = {
        "query": query,
        "office": offices.discovery_office_candidates(),
    }
    if parameter is not None:
        args["parameter"] = parameter
    return {
        "reason": "no_offices_in_scope",
        "message": (
            "No office catalog is cached yet, so nothing was searched. Retry with "
            "`office` set to this curated list (priority order; the budget takes the "
            "first few per call) and keep following `repair_hint` until "
            "`offices_skipped_for_budget` is empty."
        ),
        "tool": "cwms_search_places",
        "args": args,
    }


def _resolve_scope(
    *,
    office: str | list[str] | None,
    cursor: str | None,
    limit: int | None,
    req: str,
    use_cache: bool,
) -> tuple[dict[str, Any] | None, int, list[str], list[str], list[str]]:
    """Resolve the office set to search, honoring a continuation cursor.

    Returns `(decoded_cursor, offset, offices_searched, offices_skipped,
    partial_reasons)`. On the cursor path the office set is bounded to the
    cursor's locked list — a forged cursor cannot WIDEN the fan-out beyond
    what `coerce_offices` structurally allows (at most `MAX_CURSOR_OFFICES`
    entries) — but that list is then re-run through `_run_fanout` (#72), the
    SAME uncached-office budget the fresh path enforces, so a forged cursor
    naming many never-cached offices cannot bypass the budget either. A
    legitimate cursor only ever locks in `offices_searched` from a prior page
    (see the `next_cursor` encode call below), and those offices are normally
    still cache-hot moments later, so this re-check is a no-op cost-wise for
    the real pagination flow. It DOES introduce one new, narrow failure mode
    versus the pre-#72 behavior (which unconditionally refetched every locked
    office regardless of cache state): if cache TTL evicts more than
    `_fanout_budget()` of the locked offices between pages, some now get
    budget-skipped here. Silently shrinking the searched set and hoping
    `ensure_total` catches the resulting mismatch would be both non-deterministic
    (only fires if the total happens to differ) and could silently under-report
    results if it doesn't — so instead this raises `invalid_cursor` immediately
    whenever continuation budgeting skips ANY previously-locked office,
    labeled distinctly from a genuine catalog-shift.
    """
    if cursor is None:
        requested = _normalize_office_arg(office)
        if requested is None:
            requested = offices.cached_offices_for_locations()
        offices_searched, offices_skipped, partial_reasons = _run_fanout(
            requested, use_cache=use_cache, explicit_empty=office is not None and not requested
        )
        return None, 0, offices_searched, offices_skipped, partial_reasons

    if limit is None:
        # `cursor` implies paged access; an unlimited limit (None / CLI 0)
        # would slice a tail subset yet report has_more=false — contradictory.
        raise pagination.invalid_cursor(
            "cursor pagination requires a positive limit; omit `cursor` to fetch all results",
            offending_value=cursor[: pagination.CURSOR_ECHO_MAX],
        )
    decoded = pagination.decode_cursor(cursor)
    # Cheap checks BEFORE upstream fan-out: kind, request hash, offset shape,
    # and a bounded/typed office set (a forged cursor must not widen the fan-out).
    offset = pagination.validate_continuation(decoded, kind="search_places", req=req)
    locked_offices = pagination.coerce_offices(decoded)
    offices_searched, offices_skipped, partial_reasons = _run_fanout(
        locked_offices, use_cache=use_cache, explicit_empty=False
    )
    if offices_skipped:
        # A locked office fell out of budget (cache TTL evicted it, or the
        # cursor was forged). Reject outright rather than silently searching
        # a smaller set than the cursor promised.
        raise pagination.invalid_cursor(
            "cursor's locked office set no longer fits the per-call fan-out budget "
            "(cache expired, or the cursor is invalid); restart without `cursor`",
            offending_value=", ".join(offices_skipped)[: pagination.CURSOR_ECHO_MAX],
        )
    return decoded, offset, offices_searched, offices_skipped, partial_reasons


def _run_fanout(
    requested: list[str], *, use_cache: bool, explicit_empty: bool
) -> tuple[list[str], list[str], list[str]]:
    """Decide which offices to search; return (searched, skipped, reasons).

    Mirrors `publishers_index.publishers_for_parameter`: cached offices
    are always allowed; uncached offices consume the per-call budget. With
    `use_cache=False` every office is refetched, so none counts as cached.
    `explicit_empty` marks a caller-supplied `office=[]` (vs an empty cached
    scope), which changes the wording of the empty-scope reason.
    Offices that error during the actual search are flagged via
    `partial_reasons` in the parent response.
    """
    budget = _fanout_budget()
    searched: list[str] = []
    skipped: list[str] = []
    reasons: list[str] = []
    for office in requested:
        cached = use_cache and catalog.location_catalog_cached(office)
        if cached or budget > 0:
            searched.append(office)
            if not cached:
                budget -= 1
        else:
            skipped.append(office)
    if not requested:
        reasons.append(
            "no_offices_in_scope: explicit empty office list; nothing was searched"
            if explicit_empty
            else "no_offices_in_scope: no office catalog is cached yet; follow `repair_hint` "
            "or pass `office` explicitly"
        )
    if skipped:
        reasons.append(
            f"offices_skipped_for_budget: {len(skipped)} office(s) not searched this call; "
            "follow `repair_hint` to continue"
        )
    return searched, skipped, reasons


def _gather_enriched(
    office_ids: list[str],
    query: str,
    *,
    use_cache: bool,
    reraise_single: bool,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Run filtered enrichment per office and merge.

    Returns `(rows, error_reasons, temporary_errored)`. A failing office in a
    multi-office fan-out degrades to a `"<office>: <code>"` entry in
    `error_reasons` (surfaced as `partial_reasons` on the response) instead of
    failing the whole call. `error_reasons` entries are free-text diagnostics
    of the form ``"<office>: <token>"``; the token is either an ErrorCode value
    (e.g. ``ghost_office``) or the literal string ``internal_error
    (<ExcType>)`` for unexpected exceptions — ``internal_error`` is
    deliberately *not* an ErrorCode value. `temporary_errored` lists the
    offices whose structured error was marked `temporary` (5xx, connection
    errors); the response reports them but never re-carries them.

    When `reraise_single` is true and the resolved office set has exactly one
    entry, a structured CwmsToolsError re-raises so the full envelope —
    including the ghost-office repair hint — reaches the agent instead of an
    empty result; unexpected exceptions re-raise too. The caller passes false
    when offices were budget-skipped, so a continuation is never swallowed.
    """
    merged: list[dict[str, Any]] = []
    reasons: list[str] = []
    temporary_errored: list[str] = []
    single = reraise_single and len(office_ids) == 1
    for office_id in office_ids:
        try:
            rows = locations.search(office_id, query, use_cache=use_cache)
        except CwmsToolsError as err:
            if single:
                raise
            reasons.append(f"{office_id}: {err.envelope.code.value}")
            if err.envelope.temporary:
                temporary_errored.append(office_id)
            continue
        except Exception as exc:  # unexpected — keep the fan-out alive but visible
            if single:
                raise
            reasons.append(f"{office_id}: internal_error ({type(exc).__name__})")
            continue
        merged.extend(rows)
    return merged, reasons, temporary_errored


def _apply_parameter_filter(
    enriched: list[dict[str, Any]],
    parameter: str | None,
    *,
    use_cache: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Compute `data_at` per row and apply the optional parameter filter.

    Returns `(kept_rows, dropped_count)`. `dropped_count` is 0 when no
    parameter filter is set.

    When `parameter` is set, also pulls in *co-located siblings publishing
    the parameter* from the broader (unfiltered) office catalog, even
    when those siblings did not literally match the search query. This
    is what makes the Fremont Bridge probe succeed: `cwms_search_places(
    "Fremont Bridge", parameter="Temp-Water")` surfaces `FBLW_D1-D5,0ft`
    even though that id contains no "Fremont Bridge" string.
    """
    if not enriched:
        return [], 0

    by_office: dict[str, dict[str, dict[str, Any]]] = {}
    for r in enriched:
        by_office.setdefault(r["office_id"], {})[r["name"]] = r
    broader_by_office: dict[str, dict[str, dict[str, Any]]] = {}

    def broader(office_id: str) -> dict[str, dict[str, Any]]:
        cached = broader_by_office.get(office_id)
        if cached is not None:
            return cached
        try:
            rows = catalog.enrich_locations(office_id, use_cache=use_cache)
        except Exception:
            rows = []
        index = {r["name"]: r for r in rows}
        broader_by_office[office_id] = index
        return index

    def annotate_data_at(row: dict[str, Any]) -> None:
        """Set `row["data_at"]` from the broader catalog when this row is barren."""
        office_id = row["office_id"]
        in_office_by_name = by_office[office_id]
        siblings = row.get("co_located") or []
        data_at = _sibling_data_at(siblings, in_office_by_name, parameter)
        if not data_at and row.get("parameter_count", 0) == 0:
            broader_by_name = broader(office_id)
            target = broader_by_name.get(row["name"])
            broader_siblings = (target or {}).get("co_located") or siblings
            data_at = _sibling_data_at(broader_siblings, broader_by_name, parameter)
        row["data_at"] = data_at if row.get("parameter_count", 0) == 0 else []

    kept: list[dict[str, Any]] = []
    dropped = 0
    promoted_keys: set[tuple[str, str]] = set()
    promoted_rows: list[dict[str, Any]] = []

    for row in enriched:
        annotate_data_at(row)
        if parameter is None:
            kept.append(row)
            continue
        if parameter in (row.get("parameters") or []):
            kept.append(row)
            continue
        promoted_here = _promote_parameter_siblings(
            row, parameter, broader, promoted_keys, promoted_rows
        )
        if promoted_here and row.get("parameter_count", 0) == 0:
            # Barren parent whose siblings publish the parameter — keep
            # as an explicit discovery hint alongside the promoted siblings.
            row["data_at"] = sorted(promoted_here)
            kept.append(row)
        else:
            dropped += 1

    _append_unique_promotions(kept, enriched, promoted_rows)
    return kept, dropped


def _promote_parameter_siblings(
    row: dict[str, Any],
    parameter: str,
    broader: Any,
    promoted_keys: set[tuple[str, str]],
    promoted_rows: list[dict[str, Any]],
) -> list[str]:
    """Find broader-catalog siblings publishing `parameter`; append to the
    shared promoted-rows accumulator. Returns the names found, in input
    order, so the caller can populate `data_at` on a barren parent."""
    broader_by_name = broader(row["office_id"])
    target = broader_by_name.get(row["name"])
    co_loc = (target or {}).get("co_located") or row.get("co_located") or []
    found: list[str] = []
    for sibling_name in co_loc:
        key = (row["office_id"], sibling_name)
        if key in promoted_keys:
            continue
        sibling = broader_by_name.get(sibling_name)
        if not sibling:
            continue
        if parameter not in (sibling.get("parameters") or []):
            continue
        promoted_keys.add(key)
        sibling_copy = dict(sibling)
        sibling_copy["data_at"] = []
        promoted_rows.append(sibling_copy)
        found.append(sibling_name)
    return found


def _append_unique_promotions(
    kept: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    promoted_rows: list[dict[str, Any]],
) -> None:
    """Append promoted siblings to `kept`, skipping any name already
    surfaced as a direct query match or earlier in `kept`."""
    enriched_keys = {(r["office_id"], r["name"]) for r in enriched}
    kept_keys = {(r["office_id"], r["name"]) for r in kept}
    for sibling in promoted_rows:
        key = (sibling["office_id"], sibling["name"])
        if key in enriched_keys or key in kept_keys:
            continue
        kept.append(sibling)
        kept_keys.add(key)


def _sibling_data_at(
    siblings: list[str],
    by_name: dict[str, dict[str, Any]],
    parameter: str | None = None,
) -> list[str]:
    """Shared predicate: filter siblings to those that publish data.

    When `parameter` is set, further filter to siblings that publish that
    specific parameter. Stable ordering so snapshot tests don't churn.
    """
    out: list[str] = []
    for s in siblings:
        info = by_name.get(s)
        if not info or info.get("parameter_count", 0) <= 0:
            continue
        if parameter is not None and parameter not in (info.get("parameters") or []):
            continue
        out.append(s)
    return sorted(out)


def describe_place(
    office: str,
    name: str,
    *,
    include_locks: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_describe_place` — full Location + Project + publisher fingerprint.

    Combines up to 4 upstream calls (get_location, get_project with fallback,
    get_timeseries_catalog, freshness derivation). When any sub-call fails
    in a way we can recover from, the response carries `partial: true` and
    `partial_reasons: [...]` so the agent sees the truncation.

    `include_locks` (#134) adds `locks`/`lock_lookup_status` (and
    `lock_upstream_status` when a failed lock call carried an HTTP status)
    for PROJECT and LOCK locations — see `core.locks`. A failed or partial
    lock lookup appends `lock_lookup_failed` to `partial_reasons`; it never
    fails the describe. Without it, none of those keys are present.
    """
    location = locations.get_one(office, name, use_cache=use_cache)
    project_resp = projects.get_one(office, name, use_cache=use_cache)
    ts_ids = catalog.ts_ids_for_location(office, name, use_cache=use_cache)
    param_counts = publishers.parameter_counts(ts_ids)
    pub_facts = publishers.aggregate_publishers(ts_ids)
    freshness = catalog.freshness_for_location(office, name, use_cache=use_cache)

    response: dict[str, Any] = {
        "office_id": office,
        "name": name,
        "location": location,
        "project": project_resp.get("project_metadata"),
        "partial": project_resp.get("partial", False),
        # A new list: the project response is cached, so never mutate its own.
        "partial_reasons": list(project_resp.get("partial_reasons", [])),
        "parameters": sorted(param_counts.keys()),
        "parameter_count": len(param_counts),
        "publishers": [
            {
                "publisher": f.publisher,
                "rank": f.rank,
                "ts_count": f.ts_count,
                "parameters": list(f.parameters),
            }
            for f in pub_facts
        ],
        "ts_ids": ts_ids,
        "last_data_timestamp": freshness,
        "source_workaround": project_resp.get("source_workaround"),
        "upstream_status": project_resp.get("upstream_status"),
    }
    if include_locks:
        lock_resp = locks.lookup(office, name, location.get("location-kind"), use_cache=use_cache)
        response.update(lock_resp)
        if lock_resp["lock_lookup_status"] in {"failed", "partial"}:
            response["partial"] = True
            response["partial_reasons"].append("lock_lookup_failed")
    return response


def list_parameters(
    office: str,
    name: str,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_list_parameters` — parameters at a location, grouped by publisher.

    When the location is barren (`ts_count == 0`), the response carries a
    top-level `data_at` field listing co-located siblings that DO publish
    data — this is the common Lake Washington / UBLW_S1 case where the
    parent has no ts ids but a depth-tagged child does. When the location
    is data-bearing, `data_at` is null (no repair needed).
    """
    ts_ids = catalog.ts_ids_for_location(office, name, use_cache=use_cache)
    pub_facts = publishers.aggregate_publishers(ts_ids)
    freshness = catalog.freshness_for_location(office, name, use_cache=use_cache)
    return {
        "office_id": office,
        "name": name,
        "ts_count": len(ts_ids),
        "by_publisher": [
            {
                "publisher": f.publisher,
                "rank": f.rank,
                "parameters": list(f.parameters),
                "ts_count": f.ts_count,
            }
            for f in pub_facts
        ],
        "all_parameters": sorted(publishers.parameter_counts(ts_ids).keys()),
        "last_data_timestamp": freshness,
        "data_at": _data_at_for_location(office, name, use_cache=use_cache) if not ts_ids else None,
    }


def _data_at_for_location(
    office: str,
    name: str,
    *,
    use_cache: bool,
) -> list[str]:
    """Find data-bearing co-located siblings for a single named location.

    Used by `list_parameters` when the requested location is barren. Pulls
    the unfiltered enriched office catalog (cached) and applies the shared
    `_sibling_data_at` predicate. Returns an empty list when no sibling has
    data or when the location isn't in the office catalog at all.
    """
    enriched = catalog.enrich_locations(office, use_cache=use_cache)
    target = next((r for r in enriched if r["name"] == name), None)
    if target is None:
        return []
    siblings = target.get("co_located") or []
    if not siblings:
        return []
    by_name = {r["name"]: r for r in enriched}
    return _sibling_data_at(siblings, by_name)


def browse_region(
    *,
    office: str,
    bbox: BBox | None = None,
    state: str | None = None,
    kind: str | None = None,
    limit: int | None = DEFAULT_BROWSE_LIMIT,
    cursor: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """`cwms_browse_region` — enriched catalog filtered by office, bbox, state, or kind.

    Filtering happens client-side because CDA doesn't expose bbox or radius
    queries. Co-location is computed across the office's full catalog before
    bbox filtering so siblings outside the bbox can still be flagged.

    `limit` caps the number of results (default 50). A no-filter browse of a
    large office can return thousands of rows; the cap keeps the response
    bounded. Set `limit=None` (or `limit=0` on the CLI) for no cap. When the
    cap kicks in the response carries `has_more: true`, `total_count`,
    `next_cursor`, and a `truncation_hint`. `truncated` stays `false`: every
    row beyond `limit` is still reachable via `next_cursor`, so it's never
    "unrecoverable" for this tool (#73). Data-bearing rows sort ahead of
    ghosts so a capped browse keeps the useful records.

    Pass the opaque `next_cursor` from a prior response as `cursor` to fetch
    the next page. The cursor encodes the request fingerprint; passing a cursor
    from a different request (different office/state/bbox/kind) raises
    ``INVALID_CURSOR``.

    `kind` filters on the catalog row's `location_kind` (case-insensitive;
    rows with no kind never match). It reflects the catalog's classification,
    not membership in `/projects`. An empty/blank `kind` is a usage error.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer or None")
    if limit == 0:
        limit = None  # 0 means "no cap"

    norm_kind = _normalize_kind(kind)
    req = _browse_request_hash(office=office, bbox=bbox, state=state, kind=norm_kind)
    decoded: dict[str, Any] | None = None
    offset = 0
    if cursor is not None:
        if limit is None:
            # `cursor` implies paged access; an unlimited limit (None / CLI 0)
            # would slice a tail subset yet report has_more=false — contradictory.
            raise pagination.invalid_cursor(
                "cursor pagination requires a positive limit; omit `cursor` to fetch all results",
                offending_value=cursor[: pagination.CURSOR_ECHO_MAX],
            )
        decoded = pagination.decode_cursor(cursor)
        offset = pagination.validate_continuation(decoded, kind="browse_region", req=req)

    enriched = catalog.enrich_locations(office, use_cache=use_cache)
    rows = _filter_browse_rows(enriched, state=state, kind=norm_kind, bbox=bbox)

    # Data-bearing first, then ghosts; stable by office/name. Mirrors
    # `cwms_search_places` so a capped browse keeps the useful rows.
    rows = sorted(rows, key=lambda r: (-r["parameter_count"], r["office_id"], r["name"]))
    total_count = len(rows)
    ghost_count = sum(1 for r in rows if r["parameter_count"] == 0)
    if decoded is not None:
        pagination.ensure_total(decoded, total=total_count)  # catalog-shift guard
    if limit is None:
        rows = rows[offset:]
        has_more = False
    else:
        rows = rows[offset : offset + limit]
        has_more = offset + limit < total_count
    next_cursor: str | None = None
    if has_more and limit is not None:
        next_cursor = pagination.encode_cursor(
            {
                "v": pagination.CURSOR_VERSION,
                "kind": "browse_region",
                "off": offset + limit,
                "req": req,
                "total": total_count,
            }
        )

    # Index the FULL office catalog (pre-filter) so a barren row can name
    # co-located siblings that publish data even when those siblings fall
    # outside the bbox/state/kind filter — matching the `data_at` repair hint from
    # search and the co-location-before-filtering note above.
    by_name = {r["name"]: r for r in enriched}

    def _data_at(row: dict[str, Any]) -> list[str]:
        if row.get("parameter_count", 0) != 0:
            return []
        return _sibling_data_at(row.get("co_located") or [], by_name)

    # Drop the verbose `raw` field for region browse responses — agents asking
    # for a region overview don't need every per-row DTO. They can fetch
    # cwms_describe_place for any specific hit.
    response: dict[str, Any] = {
        "office": office,
        "bbox": _bbox_to_dict(bbox),
        "state": state,
        "result_count": len(rows),
        "ghost_count": ghost_count,
        "total_count": total_count,
        # See `search_places`'s docstring (#73): `limit` is always pageable
        # via `next_cursor`, so `truncated` stays False; `has_more` is the
        # pagination signal.
        "truncated": False,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": limit,
        "results": [
            {
                "office_id": r["office_id"],
                "name": r["name"],
                "public_name": r.get("public_name"),
                "location_kind": r.get("location_kind"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "depth": depth.parse_depth(r["name"]),
                "parameter_count": r["parameter_count"],
                "parameters": r.get("parameters", []),
                "publishers": r["publishers"],
                "last_data_timestamp": r.get("last_data_timestamp"),
                "co_located": r.get("co_located", []),
                "data_at": _data_at(r),
            }
            for r in rows
        ],
    }
    if norm_kind is not None:
        # Echo only when set, so an unset filter is an absent key (never null)
        # on both surfaces, which serialize through the same response model.
        response["kind"] = norm_kind
    if has_more:
        response["truncation_hint"] = (
            f"returned {len(rows)} of {total_count}; fetch the next page with the "
            "`next_cursor`, or pass --limit 0 for all rows"
        )
    return response


def _normalize_kind(kind: str | None) -> str | None:
    """Normalize a browse `kind` filter; a blank string is a usage error."""
    if kind is None:
        return None
    norm = kind.strip().upper()
    if not norm:
        raise CwmsToolsError.of(
            ErrorCode.USAGE_ERROR,
            "kind must be a non-empty location kind (e.g. PROJECT, LOCK, SITE).",
            field="kind",
            value=kind,
            reason="Pass a location kind, or omit kind to browse every kind.",
        )
    return norm


def _browse_request_hash(
    *, office: str, bbox: BBox | None, state: str | None, kind: str | None
) -> str:
    """Cursor fingerprint for a browse request.

    `kind` joins the hash only when set, so the no-kind hash input is
    byte-identical to the pre-#133 form and existing cursors still validate.
    """
    parts: dict[str, Any] = {"office": office, "bbox": _bbox_to_dict(bbox), "state": state}
    if kind is not None:
        parts["kind"] = kind
    return pagination.request_hash(parts)


def _filter_browse_rows(
    rows: list[dict[str, Any]], *, state: str | None, kind: str | None, bbox: BBox | None
) -> list[dict[str, Any]]:
    """Apply the browse state/kind/bbox filters (an AND) to enriched catalog rows."""
    if state:
        target = state.upper()

        # CDA returns the two-letter state code as `state` on the catalog row
        # and as `state-initial` on the single-location response. Check both
        # so the filter works regardless of which shape arrived.
        def _matches_state(r: dict[str, Any]) -> bool:
            s = _row_state(r)
            return s is not None and s.upper() == target

        rows = [r for r in rows if _matches_state(r)]

    if kind is not None:
        # `kind` is already normalized (uppercased); rows with no kind never match.
        rows = [
            r for r in rows if isinstance((k := r.get("location_kind")), str) and k.upper() == kind
        ]

    if bbox is not None:
        geopoints = [
            GeoPoint(office_id=r["office_id"], name=r["name"], latitude=lat, longitude=lon)
            for r in rows
            if isinstance((lat := r.get("latitude")), (int, float))
            and isinstance((lon := r.get("longitude")), (int, float))
        ]
        in_bbox = {(g.office_id, g.name) for g in filter_by_bbox(geopoints, bbox)}
        rows = [r for r in rows if (r["office_id"], r["name"]) in in_bbox]
    return rows


def _bbox_to_dict(bbox: BBox | None) -> dict[str, float] | None:
    if bbox is None:
        return None
    return {"south": bbox.south, "west": bbox.west, "north": bbox.north, "east": bbox.east}


def _row_state(row: dict[str, Any]) -> str | None:
    """Pluck a two-letter state code from a catalog row, tolerating either shape."""
    raw = row.get("raw") or {}
    for key in ("state-initial", "state"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            return v
    return None


__all__ = ["browse_region", "describe_place", "list_parameters", "search_places"]
