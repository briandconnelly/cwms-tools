# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Agent-friendliness contract fixes from a cross-model (Claude + Codex) review of
the MCP server and CLI.

### Added

- **`cwms_browse_region` / `region browse` now cap results** with a `limit`
  (default 50; `--limit`/`-n` on the CLI, `0` for no cap). Responses carry
  `total_count`, `truncated`, `limit`, and a `truncation_hint`, and data-bearing
  rows sort ahead of ghosts so a capped browse keeps the useful records. Closes
  the unbounded-list hazard where a no-filter browse of a large office could
  return thousands of rows.
- **Browse results now carry `parameters` and `data_at`**, matching
  `cwms_search_places`. Previously `BrowseRegionResponse.results` was typed as
  `PlaceSummary` (which declares both) but never populated them.
- **`SearchPlacesResponse` now declares `total_count`/`truncated`/`limit`** as
  schema fields so the MCP output schema documents the pagination the tool
  already returned (it had relied on `extra="allow"`).
- **`cwms://capabilities` now publishes a per-tool error catalog**
  (`tool_error_codes`): the `error.code` values each tool can return, so an
  agent can branch per tool instead of against the global enum. The per-tool
  codes are folded into each tool's fingerprint definition.

### Changed

- **CLI structured errors now go to stderr in one consistent shape.** Every
  command emits failures as the full `{ok: false, error: {...}}` envelope (with
  `code`, `request_id`, `hint`, `repair`, …) on stderr; stdout stays
  success-only. Replaces three divergent shapes (full envelope, a hand-built
  partial dict, and a string-valued `error`). The bulk `value get` aggregate
  remains the stdout payload (per-item failures inline, non-zero exit), now
  declared as an explicit exception in `cwms-tools schema`'s `machine_profile`
  (`success_stream` / `error_stream` / `error_stream_exceptions`).
- **MCP error channels are now consistent.** The `cwms_get_overview_section`
  tool's miss returns the same in-band `{ok: false, error: {...}}` envelope
  (code `not_found`) as the seven task tools, replacing its bespoke
  `{error, repair}` shape. Overview `resources/read` misses now raise a JSON-RPC
  error carrying `machine_code`/`human_message`/`repair`/`recoverable` in
  `error.data`, instead of returning an error-shaped 200 body that didn't match
  the section schema. `cwms://capabilities` documents both channels under
  `error_handling` (tool errors discriminate on `ok`, not the protocol `isError`
  flag, which FastMCP cannot set alongside structured content).
- `cwms_publishers_for_parameter` coverage now distinguishes
  `offices_error_skipped` (catalog fetch errored) from
  `offices_skipped_for_budget` (hit the per-call fanout budget), so the agent
  can tell a retry case from a "re-run to continue indexing" case. The internal
  per-office handler now catches `CwmsToolsError` specifically rather than bare
  `Exception`, so genuine bugs surface instead of being silently absorbed into
  coverage.

### Removed

- Dropped the `timeout` and `catalog_cursor_invalidated` error codes. They were
  advertised in the capability summary, CLI schema, exit-code map, and
  fingerprint but never emitted by any code path. Removing them keeps the
  advertised error surface honest. (Both were unreachable; this changes the
  capability fingerprint.)

### Fixed

- **Capability fingerprint is now identical across surfaces and covers tool
  schemas.** Previously `cwms://capabilities` hashed an empty tool set while the
  CLI `fingerprint` command and each tool's `source.fingerprint` hashed tool
  names only, so the three disagreed and a schema change did not move the
  fingerprint — defeating `fingerprint_scope: "schema-contract"`. A new
  `mcp/contract.py` extracts the real registered tool definitions (input/output
  schemas + annotations) once and feeds the single `canonical_fingerprint()`
  used by all three surfaces.
- **HTTP 429 is now classified as `rate_limited`** (retryable) with
  `retry_after_ms` parsed from the upstream `Retry-After` header, instead of the
  previous non-retryable `upstream_error` — so a backing-off agent waits and
  retries instead of giving up.
- **`publisher for-parameter` no longer leaks tracebacks.** It now wraps core
  failures like its sibling commands, so a propagating error becomes a
  structured envelope with the mapped exit code instead of an uncaught
  traceback on exit 1.
- **`value history` reports the precise offending field** (`begin` or `end`) on
  a bad timestamp instead of the lumped `begin/end`.

## [0.1.0] - 2026-05-19

Initial public release. Agent-friendly read-only tools for the USACE
[CWMS Data API](https://cwms-data.usace.army.mil/cwms-data/), exposed
as both a [FastMCP 3](https://gofastmcp.com/) server and a
[Typer](https://typer.tiangolo.com/) CLI over one behavioral core.

### Added

- **MCP server** (`cwms-tools mcp serve --transport stdio|streamable-http`)
  with eight tools and four resources:
  - Tools: `cwms_search_places`, `cwms_describe_place`,
    `cwms_list_parameters`, `cwms_browse_region`, `cwms_get_value`,
    `cwms_get_history`, `cwms_publishers_for_parameter`,
    `cwms_get_overview_section`. All declared `readOnlyHint: true`.
    Every task tool returns a concrete pydantic v2 model so FastMCP
    derives a full `outputSchema` (properties, types, nested
    `PlaceSummary` / `ActiveThreshold` shapes).
  - Resources: `cwms://capabilities`, `cwms://overview` (index),
    `cwms://overview/{section_id}{?detail}` (RFC 6570 query-param
    template), `cwms://overview/{section_id}/chunk/{chunk_id}`.
    (`cwms://offices` and `cwms://parameters` deferred to v0.2 along
    with their backing data sources.)
  - Every tool accepts a `detail: summary | full` toggle that changes
    response density (not shape).
  - Every successful task response carries `source.fingerprint`
    (the capability fingerprint at call time), `source.workaround`
    (set when a cwms-python bug mitigation fired), `source.endpoints_called`,
    and `source.cached`.
- **CLI** (`cwms-tools`):
  - Inspection affordances: `whoami`, `env`, `config show --resolved`,
    `fingerprint`, `schema` (machine-readable command tree + error
    codes + exit-code map).
  - Task tools: `place {search,describe,parameters}`, `region browse`,
    `value {get,history}`, `publisher for-parameter`.
  - Global flags: `--machine` / `--json`, `--no-cache`, `--isolated`.
    Auto-enables machine mode on non-TTY stdout.
  - `mcp serve` subcommand with `_StdoutGuard` so stdio MCP traffic
    can't be corrupted by stray writes from logging or rich.
- **Catalog enrichment**: every search/browse result carries
  `parameter_count` (ghost detection), `publishers` (ranked by
  trust), `last_data_timestamp` (freshness), and `co_located` (other
  ids within ~100 m).
- **NW District stub repair**: requests against `NWO/NWK/NWS/NWP/NWW`
  short-circuit with `error.code = ghost_office` and a `repair` hint
  pointing at `NWDM` or `NWDP`.
- **Wrapper landmines covered**:
  - cwms-python issue #286 (seasonal levels): the seasonal-level
    workaround in `core/_workarounds.py` routes around
    `get_level_as_timeseries` and hits `/levels/{id}/timeseries`
    directly; responses carry `source_workaround: "issue-286"`.
  - `get_project` format-error fallback to the underlying Location
    with `partial: true, partial_reasons: ["get_project_format_error"]`.
  - `get_timeseries` silent truncation at 300 000 points detected
    and surfaced as `truncated: true` with a `truncation_hint`.
  - Multithread fan-out disabled at the wrapper call site; concurrency
    owned by a single bounded `ThreadPoolExecutor` (default 8 workers,
    `CWMS_TOOLS_WORKERS` overrides).
- **Capability fingerprint** (SHA-256) over: cwms-tools + cwms-python
  versions, tool inventory + schemas, resource catalog, error codes,
  bundled cwms-overview.md SHA, session config, active workarounds.
  Exposed at `cwms://capabilities`, `cwms-tools fingerprint`, and
  in `source.fingerprint` on every tool response.
- **Two-tier cache** (in-memory LRU + `diskcache`) rooted at
  `platformdirs.user_cache_dir("cwms-tools")`, overridable by
  `CWMS_TOOLS_CACHE_DIR`. Namespace TTLs:
  - `offices` / `parameters`: 7 d
  - `location_catalog` / `ts_catalog`: 6 h
  - `levels`: 24 h, keyed by `(level_id, office, effective_date)`
  - `timeseries`: not cached (live data)
- **Upstream-server etiquette**: descriptive `User-Agent`
  (`cwms-tools/<v> (+<repo>) cwms-python/<v>` plus
  `CWMS_TOOLS_USER_AGENT_EXTRA`), right-sized `pool_connections =
  max(2 * MAX_WORKERS, 16)`, optional `From:` header via
  `CWMS_TOOLS_OPERATOR_EMAIL`, `Retry-After` honored by
  `cwms-python`'s retry stack. No background scans or pre-warming.
- **Bundled overview**: `cwms-overview.md` ships under
  `cwms_tools/data/`, parsed at runtime into stable section slugs
  with 8 KB chunked bodies and stable chunk IDs.
- **Test suite**: 240 tests (unit + mocked integration via
  `responses` against the `requests`-backed cwms-python) and a
  fingerprint snapshot suite that pins the v0.1.0 tool/resource/
  error-code surface.

### Changed

- `PlaceSummary` and `ListParametersResponse` now declare `data_at` as a
  schema field with a description, so agents reading the response schema
  see the repair hint. Previously the field reached clients only through
  `extra="allow"` (addresses Codex review F3).
- `cwms_search_places` `office` is now optional and accepts `str | list[str]`
  (addresses Codex review F1). When omitted, the search fans out across
  offices already cached this session; explicit lists widen the scope.
  New (uncached) offices are capped per call by a small fanout budget;
  the response carries `offices_searched` and `offices_skipped_for_budget`
  with an embedded repair hint so the agent can widen deterministically.
  CLI exposes this as a repeatable `--office`/`-o` flag.
- `cwms_search_places` adds an optional `parameter` filter that drops
  non-publishing rows from `results` and surfaces co-located siblings
  that DO publish the parameter — even when those siblings did not
  literally match the natural-language query (addresses Codex review
  F2; this is the Fremont Bridge probe fix). The response carries
  `nearby_non_matching_count` so the agent sees how much was filtered
  without paying for the filtered rows themselves.
- `PlaceSummary` now declares a `parameters: list[str]` field — the
  distinct CWMS parameter codes published at each location — sourced
  from the enriched catalog. Empty for barren/ghost rows.
- `cwms_search_places` `data_at` lookup now falls back to the full
  office catalog when an in-result sibling does not match the natural
  query, so a parent like `FBLW` can name its `FBLW_D1-*` depth-tagged
  temperature sensors even when those names never matched.
- `unit` is now a closed set (`'EN'` or `'SI'`) on both surfaces: MCP
  tools use `Literal["EN", "SI"]` so FastMCP/pydantic rejects unknown
  values before the tool body runs; CLI uses a `Unit(str, Enum)` so
  Typer surfaces the same choice validation. Parameter descriptions
  for the free-form `parameter` field gained richer examples and a
  pointer at `cwms_list_parameters` for discovery (addresses Codex
  review F5).
- MCP error envelope normalization: the two manual validation branches
  (`cwms_browse_region` partial-bbox and `cwms_get_history` datetime
  parse) now flow through the full `CwmsToolsError.of(...)` envelope via
  a new `_envelope_ref` helper, so agents see `request_id`,
  `offending_value`, `hint`, and `source` on these errors just like
  every other failure (addresses Codex review F4). Datetime parsing is
  split into separate `begin_iso` / `end_iso` blocks; the response
  `field` now names the offending field precisely instead of the
  lumped `"begin_iso/end_iso"`.

### Known limitations (v0.1.0)

- Forecast retrieval (`cwms_get_forecast`) deferred to v0.2 — the
  forecast publisher conventions are an open empirical question.
- `--filter` / `--field` / `--sort` flags on list outputs and
  `--allow-partial` on multi-id `value get` deferred to v0.2.
- `CWMS_TOOLS_MAX_RPS` declared in the schema but not enforced in
  v0.1.0; the bounded executor caps concurrency below any plausible
  per-host RPS limit.
- No global reverse-index build (parameter → publishers across all
  ~68 offices) in v0.1.0; `cwms_publishers_for_parameter` answers
  from cached + bounded-fetch offices only.

[0.1.0]: https://github.com/briandconnelly/cwms-tools/releases/tag/v0.1.0
