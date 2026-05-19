# Plan — eval-driven fixes (post v0.1.0 hardening)

## Context

Two parallel live-CLI evaluations surfaced eight defects beyond the
ones fixed during the eval itself. Six are real bugs (some pre-existing,
some introduced by mid-eval fixes); two are UX gaps.

Already landed in this branch (`main`):

| Commit | Fix |
|---|---|
| `00f4ca9` | Dedupe duplicated catalog rows; read freshness from `extents[].latest-time` |
| `45c6ed4` | Make `include_extents` opt-in; scope `ts_ids_for_location` server-side |
| `4e969af` | Time-budget threshold lookup; fix state filter; fire NW-stub check on `place describe` |

Open issues, ordered by severity:

| # | Severity | Issue |
|---|---|---|
| C | **high (regression)** | Broad `place search` (many name matches) → oversized alternation regex → CDA 500 → unwrapped `ApiError` surfaces a Rich traceback to stderr. Introduced by `45c6ed4`. |
| D | **high** | `place describe` against any non-project location (e.g. `NWDP/UBLW`, `NWDP/UBLW_S1-D21,0ft`) fails with `upstream_error` from the project lookup instead of degrading to `partial: true`. Pre-existing. |
| A | **medium** | `level_lookup_status: "timed_out"` is the common case at 8 s — even on small offices. Inline status classification almost never fires on cold cache. |
| E | **medium** | No CLI flag / MCP-tool param to skip classification entirely. Agents who want a fast value-only fetch have to wait for the timeout. |
| B | **medium** | `cwms-python` writes `ERROR:root:CDA Error: response=<Response [406]>` to stderr even on recovered errors (the project format-error fallback path). Noise for agent stderr capture. |
| G | **medium** | Depth-suffixed children are hard to discover. `place parameters NWDP/UBLW_S1` returns zero; data lives at `UBLW_S1-D21,0ft`. Search results don't hint at this. |
| F | **low** | No "search all offices" / "which office owns this place" entry point. |
| H | **low** | `value get --help` lacks a comma/depth example like `NWDP/UBLW_S1-D21,0ft/Temp-Water`. |

## Goals

- **Eliminate the regression (#C) and the hard-failing describe path (#D).** These break realistic workflows today.
- **Make classification a first-class density toggle (#A, #E).** Default the threshold lookup off; opt in with `--with-status` when the caller is willing to wait.
- **Silence cwms-python's `ERROR:root:` writes (#B).** Agents shouldn't have to parse around them.
- **Help agents discover depth-tagged children (#G).** When a search hit is a barren parent but a sibling has data, surface a repair hint.
- **Polish docs (#H).** One realistic example per command.

Non-goals for this round:

- Cross-office name resolution (#F). It's a useful feature but a separate
  effort — needs catalog pre-warming across offices and a new tool
  shape. Punt to v0.2.

## Fixes

### #C — Cap the alternation regex; wrap upstream `ApiError`

**Root cause.** `enrich_locations`, when given a `like` filter, builds
`^(name1|name2|…|nameN)\.` for the ts-catalog query. For "Bridge" in
NWDP this is ~100 matches × ~25 chars each = ~3 KB before
URL-encoding, which the CDA endpoint rejects with a 500. The upstream
`cwms.api.ApiError` then propagates to the CLI adapter, which dumps a
Rich traceback because `_safe()` only catches `CwmsToolsError`.

**Fix.**

1. **Gate the alternation by URL-encoded length, not by row count,
   in `core/catalog.py::enrich_locations`.** Compute the candidate
   `^(name1|…|nameN)\.` regex; if its URL-encoded form would push the
   ts-catalog request URL above `MAX_TS_LIKE_BYTES` (start at 2048 —
   well under the empirical CDA threshold but verifiable), **skip
   ts-catalog enrichment entirely for that response**. The location
   rows still carry `publishers: []`, `parameter_count: 0`,
   `last_data_timestamp: null`, plus an explicit
   `enrichment_truncated: true` flag and `enrichment_truncated_reason:
   "alternation_overflow"`. Agents can re-issue a narrower query.
   (Rejected alternative: a second unscoped fetch — it pays a
   different but still-large cost, and the CDA threshold for the
   unscoped path is unverified.)
2. **Wrap upstream errors in `core/catalog.py::get_locations_catalog`
   and `get_timeseries_catalog`.** Inspect any non-2xx
   `cwms.api.ApiError` via `exc.response.status_code` and map:
   - 5xx → `CwmsToolsError(UPSTREAM_ERROR, retryable=True)`
   - 4xx other than 404 → `CwmsToolsError(UPSTREAM_ERROR,
     retryable=False)`
   - 404 → `CwmsToolsError(NOT_FOUND, retryable=False)`
   In all cases, include the offending URL in `endpoints_called`.
3. **Also wrap `core/locations.py::get_one`** — it has its own bare
   `try/except Exception` that re-raises as `NOT_FOUND` for everything.
   Apply the same status-code-based classification.

Tests: `test_catalog.py` gets two new cases — `_skips_enrichment_on_alternation_overflow`
(synthesize names long enough that the encoded URL exceeds
`MAX_TS_LIKE_BYTES`; assert `enrichment_truncated: true` and **no**
ts-catalog call is made) and `_wraps_upstream_500_as_error`
(500 → `CwmsToolsError(UPSTREAM_ERROR, retryable=True)`). Also
`test_locations.py::_wraps_500_as_upstream_error_not_not_found`.

### #D — `place describe` on a non-project location degrades to `partial`

**Root cause.** `core/projects.py::get_one` catches only the documented
406 format-error string and falls back to the location payload. Every
other `ApiError` raises `UPSTREAM_ERROR`, which `places.describe_place`
re-raises and the CLI adapter exits 9. The eval confirms the failure
on `NWDP/UBLW` and `NWDP/UBLW_S1-D21,0ft` but doesn't pin the exact
status code CDA returns for "this location isn't a project."

**Fix.** Switch from string-matching to status-code inspection on
`ApiError.response.status_code`:

| Upstream | Treatment |
|---|---|
| 406 with `"No Format for this content-type and data-type"` | existing format-error fallback; `partial_reasons: ["get_project_format_error"]` |
| 404 (CDA likely returns this for non-project location) | new fallback; `partial_reasons: ["not_a_project"]` |
| Any other 4xx | new fallback; `partial_reasons: ["project_lookup_4xx"]` plus the status code captured in `source.upstream_status` |
| 5xx | `CwmsToolsError(UPSTREAM_ERROR, retryable=True)` — not recoverable into a partial response |

**Schema dependency.** The partial-success path returns a success
envelope, not an error envelope, so the new field belongs on
`SourceMeta` in `src/cwms_tools/core/models.py` — not on `SourceInfo`
in `core/errors.py` (which is used only by `ErrorEnvelope`).

Concretely:
1. Add `upstream_status: int | None = None` to `SourceMeta` in
   `src/cwms_tools/core/models.py`.
2. Have `core/projects.py::get_one` include `upstream_status` in
   its returned dict on the `project_lookup_4xx` and (optionally,
   for symmetry) `not_a_project` branches.
3. Have `core/places.py::describe_place` thread that field through
   into the response.
4. Update `src/cwms_tools/mcp/tools.py::_source()` (currently accepts
   only `workaround`) to also accept and forward `upstream_status`,
   and have `cwms_describe_place` pass it in.
5. Optional but recommended: also add `upstream_status: int | None`
   to `SourceInfo` in `core/errors.py` for symmetry, so the
   `UPSTREAM_ERROR` envelopes for 5xx can carry the same field. Not
   strictly required by this plan but cheap and symmetrical.

Capability fingerprint moves on this commit.

The Location data already comes from `locations.get_one` upstream of
`projects.get_one`, so the partial response is still useful.

Tests: two new cases — `_returns_partial_when_project_404` and
`_returns_partial_when_project_other_4xx`. Live verification with
`NWDP/UBLW` after the change confirms which status code CDA actually
emits; the test fixture is calibrated against the live observation
during the re-eval.

### #A + #E — Default classification off; opt in via `--with-status`

**Root cause.** The CWMS `/levels` endpoint is reliably slow (8 s
budget exceeded on every cold-cache call I tested). The plan's
"agent-friendly one tool call with inline status" framing produces
`status_class: "unknown"` 95% of the time, which is no signal.

**Fix.**

1. In `core/values.py::get_value`, flip the default to
   `classify_against_levels: bool = False`.
2. CLI: add `cwms-tools value get … --with-status` (flag, default
   off). When set, `classify_against_levels=True` is passed through
   and the existing 8 s daemon-thread budget applies. Document
   plainly in the help that this is a slow path that may time out.
3. MCP: add `with_status: bool = False` to `cwms_get_value` with the
   same semantics. Update the tool description: "By default, returns
   the value only (fast). Set `with_status=true` to also classify the
   observation against applicable thresholds; that path is slow and
   often exceeds the budget — the response carries
   `level_lookup_status` so the agent can see what happened."
4. When `classify_against_levels=False`, the response carries
   `status_class: "unknown"` (the existing `StatusClass` enum value;
   no new enum member) and `level_lookup_status: "skipped"`. The
   `level_lookup_status` field is the load-bearing signal — agents
   should consult it to distinguish "skipped on purpose" from
   "attempted but unavailable."
5. **Tool/CLI descriptions are part of this change.** Update the
   `cwms_get_value` description in `src/cwms_tools/mcp/tools.py` and
   the `value get` help text in `src/cwms_tools/cli/commands/value.py`
   to reflect the new default. The agent-facing capability fingerprint
   includes tool schemas, so this also bumps the fingerprint.

Tests: update `test_cli_value.py` and `test_mcp_tool_handlers.py` so
the existing happy-path assertions don't depend on the threshold
lookup; add `_with_status_flag_enables_classification` and
`_default_returns_status_unknown_with_lookup_skipped`. The capability
fingerprint snapshot test is expected to fail on this commit — the
expected fingerprint is regenerated and committed in the same PR.

### #B — Quiet cwms-python's `logging.error` writes

**Root cause.** `cwms-python`'s `cwms/api.py` calls plain
`logging.error("CDA Error: …")` (the module-level function, not a
named logger). That routes to the **root** logger, not a `cwms.api`
logger — so attaching a filter to a `cwms.api` named logger does
nothing. We *recover* from the 406 format-error and the
404-not-a-project cases, but the error still hits the root logger
and lands on stderr.

**Fix.** In `core/session.py::configure_session`, attach a
`logging.Filter` to the **root logger** that drops `LogRecord`s whose
`record.pathname` resolves to the installed `cwms/api.py` file.
Resolution: `Path(cwms.api.__file__).resolve()` at configure time,
captured in the filter closure; compare against
`Path(record.pathname).resolve()` per record. Filtering on `pathname`
alone (not on `record.module`, which is just `"api"` for any module
named `api.py` and would muzzle unrelated libraries). The filter is
installed exactly once on session configure, idempotent on repeat
calls, and exposed for removal in tests.

Tests:
- `tests/test_session.py::test_configure_session_silences_cwms_api_error_log`
  uses `caplog.at_level(...)` + an explicit
  `logging.LogRecord(pathname="…/cwms/api.py", level=ERROR, …)` to
  verify the filter drops the record.
- `tests/test_session.py::test_filter_does_not_drop_other_modules`
  uses a synthetic record from a different `pathname` to verify we
  don't accidentally muzzle other libraries' error logs.

### #G — "Depth-tagged child has data" repair hint on barren parents

**Root cause.** Lake Washington WQ sensors live at
`UBLW_S1-D21,0ft`-style ids. A naive `place search "University Bridge"`
hit returns `UBLW_S1` with `parameter_count: 0`, but `UBLW_S1-D3,0ft`
(co-located) has the actual data. Users have to walk the
co_located list manually. The same dead-end hits `place parameters
NWDP/UBLW_S1` (also zero results, no hint).

**Fix.** Add a `data_at` field to the response shape in **both**
`core/places.py::search_places` and `core/places.py::list_parameters`.
The helper logic — given a (office, name) with zero params, find
co-located siblings with `parameter_count > 0` — lives in a shared
function in `core/places.py` so both paths use the same definition.

For `search_places`, populate `data_at` on each barren result row.
For `list_parameters`, populate `data_at` at the top level of the
response (the response is for a single location, so a single field).

Tests:
- `test_places.py::test_search_results_carry_data_at_repair_for_barren_parents`
- `test_places.py::test_list_parameters_carries_data_at_repair_on_barren_location`

Both with two co-located rows where the parent has zero params and
the child has one.

### #H — Realistic-id examples in help text

**Fix.** Update CLI `value get --help` and the MCP
`cwms_get_value` Annotated param descriptions to include a worked
example with a comma/depth-tagged id:
`NWDP/UBLW_S1-D21,0ft/Temp-Water`. Same for `value history`.

Tests: snapshot tests that pin the `--help` output for `value get`
and `value history`.

## Order of operations

Single feature branch `fix/eval-followups`. One commit per issue
group so review is reversible per fix.

1. #C — regex cap + ApiError wrapping (highest blast-radius issue)
2. #D — non-project describe degrades to partial
3. #A + #E — classification opt-in
4. #B — silence cwms-python logging
5. #G — `data_at` hint
6. #H — help-text polish + snapshot tests

Each commit lands with its own tests. After all six, re-run the live
eval against the same offices/places (NWDM/FTPK, SWT/FOSS, NWDP/UBLW
+ depth children) and confirm:
- `place search "Bridge" --office NWDP` returns with
  `enrichment_truncated: true` when alternation would overflow, and a
  structured envelope (not a traceback) if the upstream still 500s
- `place describe NWDP/UBLW` returns `partial: true,
  partial_reasons: ["not_a_project"]` and CDA's actual status code
  is captured in `source.upstream_status` for future calibration
- `value get NWDM/FTPK/Elev` returns in <1 s with
  `status_class: "unknown", level_lookup_status: "skipped"`
- `value get NWDM/FTPK/Elev --with-status` returns within the budget
  (8 s) with `level_lookup_status: "timed_out" | "computed" |
  "unavailable"`
- No `ERROR:root:` lines on stderr for recovered errors (filter
  active at the root logger)
- `place search "University Bridge"` includes `data_at` on the
  parent rows that point at depth-tagged children
- `place parameters NWDP/UBLW_S1` includes a top-level `data_at`
  field pointing at the depth-tagged children

## Verification matrix

| Command | Expected |
|---|---|
| `cwms-tools place search "Bridge" --office NWDP` | exits 0; if upstream 500s, returns structured envelope (not a traceback) |
| `cwms-tools place describe NWDP/UBLW` | exits 0; `partial: true, partial_reasons: ["not_a_project"]` |
| `cwms-tools place describe NWDM/FTPK` | exits 0; `partial: true, partial_reasons: ["get_project_format_error"]` (unchanged) |
| `cwms-tools value get NWDP/UBLW_S1-D21,0ft/Temp-Water` | exits 0; value-only response, no classification |
| `cwms-tools value get NWDP/UBLW_S1-D21,0ft/Temp-Water --with-status` | exits 0; response includes `level_lookup_status` |
| `cwms-tools place describe NWO/BECR` | exits 12; `error.code: ghost_office` (unchanged) |

## Sources

- `evaluation-codex-1.md` (Codex CLI evaluation, bugs C/D/E/F/G/H)
- This-session live evaluation transcript (bugs A/B and confirming C)
