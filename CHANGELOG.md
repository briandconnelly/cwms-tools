# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Installing on an Intel (x86_64) Mac no longer requires building
  `cryptography` from source, which needs a Rust toolchain. cryptography 49+
  ships only arm64 macOS wheels, and it arrives transitively through FastMCP's
  auth dependencies. cwms-tools now declares `cryptography<49` for Intel Macs
  (including x86_64 Python under Rosetta), so `uvx` installs and the `.mcpb`
  bundle there use a prebuilt 48.x wheel. Every other platform requires
  `cryptography>=50`, which includes the fix for CVE-2026-69247. `uv.lock`
  carries both versions, one per platform.
- The MCP server failed at startup with `ImportError: cannot import name
  'McpError' from 'mcp'` whenever it was installed fresh (e.g. the Claude
  Code/Codex plugin's `uvx cwms-tools==0.6.0 mcp serve`). 0.6.0 declared
  `fastmcp>=3.4.2` with no upper bound, so new installs resolved FastMCP 4.0
  and MCP SDK v2, which renamed `McpError` to `MCPError`.
- `place describe` / `cwms_describe_place` now return project metadata.
  With cwms-python 1.0.8 and earlier, every project lookup failed upstream
  with a 406 (`No Format for this content-type and data-type`), not just
  NWDM/FTPK, so `project` was always `null` and the response was
  `partial: true` with `get_project_format_error`. cwms-python 1.0.9 requests
  the v1 JSON format that CDA can serve.

### Changed

- Now requires `cwms-python>=1.0.9`.
- Now requires `cryptography>=50` on every platform except Intel Macs, which
  are capped at `cryptography<49` (see Fixed).
- `describe` summary mode prunes `project` to `project-owner` and
  `authorizing-law`. `--detail full` / `detail="full"` returns the upstream
  project verbatim, including its nested `location` — whose
  `latitude`/`longitude` CDA reports as 0.0 where the Location record has no
  coordinates, so use the top-level `location` instead.
- Retired the `project_format_error_fallback` workaround and the
  `get_project_format_error` partial reason. A 406 from `/projects` now
  degrades like any other 4xx (`project_lookup_4xx`). The capability
  fingerprint moves because the active-workaround list changed.

- Now requires FastMCP 4 (`fastmcp>=4.0.3,<4.1`, built on MCP SDK v2).
  FastMCP allows breaking changes in minor releases, so the bound admits only
  4.0.x patch releases; later minors are adopted deliberately after
  re-verification instead of reaching fresh installs unannounced. Resource-miss errors are now constructed the SDK v2 way, and the
  contract introspection reads the snake_case protocol fields.
- **Breaking (wire):** a `resources/read` for a missing overview section or
  chunk now returns JSON-RPC error code `-32602` (`INVALID_PARAMS`, per
  SEP-2164) instead of `-32002`. This matches the code FastMCP 4 itself
  returns for an unknown resource URI, so the server reports "not found" with
  a single code. The `error.data` envelope (`machine_code: "not_found"`,
  `repair`, ...) is still the documented discriminator; it gains a `uri`
  field naming the missing resource exactly as requested (query string
  included), which SEP-2164 recommends.
- The capability fingerprint moves: it folds in the FastMCP baseline this
  server was verified against, which is now 4.0.3.

## [0.6.0] - 2026-07-04

### Changed

- **Breaking:** migrated the shared `ErrorEnvelope` (tool `structuredContent`,
  resource JSON-RPC `error.data`, and CLI stderr — all three carriers) to the
  field shape this repo's own `agent-friendly-mcp` skill checklist mandates,
  deliberately deferred out of #64/PR #77: `retryable` is now `temporary`;
  `field`/`offending_value`/`hint` collapse into a single `details: {field,
  value, reason}` object, present only when at least one member is
  meaningful; `repair: {tool, args}` is now `repair: {next_step, tool,
  arguments, alternative}`; and a new optional `rate_limit_remaining` field
  is reserved for a future upstream signal — omitted from responses (not a
  fabricated `null`) until one backs it. No compatibility shim — this is a
  single breaking migration (pre-1.0, no external SDK consumers to preserve compat for),
  matching the precedent set by #77's resource error-code rename. The
  capability fingerprint moves accordingly since it hashes live output
  schemas and capability prose. Closes #76.
- Trimmed the serialized `tools/list` payload every preloading MCP client
  pays before its first call: ~81.4k chars (~20.4k tokens) down to ~54.1k
  chars (~13.5k tokens), a 34% reduction. The `ErrorEnvelope` schema — full
  and identical in every tool's outputSchema because FastMCP can't flatten
  the `SomeResponse | ErrorRef` return-type Union — is now a single shared
  compact shape (`mcp/output_schema.iserror_output_schema()`, still
  enumerating every `ErrorCode` and the `repair`/`source` nested field
  shapes so resource-blind clients keep the full branch-key set and repair
  contract inline), and tool docstrings, param descriptions, and
  response-field descriptions were tightened throughout to cut duplicated
  prose (the "discover office codes" sentence, repair-hint restatements,
  etc.) without dropping any selection/repair content. A new CI test
  (`test_tools_list_budget.py`) guards the total against regression.
  Closes #67.
- CLI and MCP response `detail` shaping is now defined once in
  `core/shaping.py` and imported by both surfaces, replacing the two drifted
  copies (`mcp/tools.py` `_shape_*` and the inline pruning in each CLI command)
  that caused #45 and #55. Behavior is unchanged; a new CLI↔MCP parity test
  asserts the two surfaces agree field-for-field in both `summary` and `full`
  modes for every tool, so they cannot silently diverge again. Closes #56.

### Fixed

- CLI `place search` and `region browse` ghost-office repair hints named the
  MCP tools (`cwms_search_places` / `cwms_browse_region`) instead of the
  runnable CLI commands, so an agent following the mechanical retry contract
  (#69) invoked a non-existent command and got "no such command" — the exact
  loop #69 was meant to make work. They now name `cwms-tools place search` /
  `cwms-tools region browse`, matching the sibling describe/parameters/value
  commands. The two CLI tests that had locked in the buggy tool names are
  corrected.
- `cwms_get_history` / `cwms-tools value history` raw-point cap
  (`_cap_raw_points`) sorted points by lexicographic comparison of the raw
  timestamp string, not chronologically — so mixed fractional-second
  precision (`…:00Z` vs `…:00.500Z`, where `'.' < 'Z'`) or mixed zone offsets
  could cap on the wrong boundary and derive a `next_begin` that skips or
  duplicates points on continuation. Sorting now keys on the parsed epoch (via
  a shared `_parse_point_timestamp` helper also used by `_next_begin_from_points`).
- The `ghost_office` error envelope was built byte-for-byte identically in
  `core.locations` and `core.catalog`; both now build it from a single shared
  `core.offices.ghost_office_error`, so the guidance text can't drift between
  the single-location-read path and the catalog path (matching how #69 already
  consolidated `NW_STUBS`/`ghost_office_repair`).
- The cold-cache fan-out budget was duplicated verbatim in
  `core.places._fanout_budget` and `core.publishers_index._budget`; both now
  delegate to a single `core.concurrency.fanout_budget`, so tuning the ratio
  can't leave `search_places` and `publishers_for_parameter` enforcing
  different per-call caps.
- The `ErrorEnvelope` carried endpoint provenance twice — a flat top-level
  `endpoints_called` and `source.endpoints_called`, populated identically — so
  every error emitted the same list on the wire twice. Dropped the flat
  top-level field (and its output-schema entry); provenance now lives solely
  under `source`, matching #70's success-side decision to keep it nested.
- Removed a dead `_threading = threading` alias in `core.values` whose comment
  claimed it kept the import live, though `threading` is used directly.

- `cwms-tools schema` (the agent-facing machine contract) was missing a
  `cwms-tools value profile` entry entirely, and `cwms-tools value history`'s
  entry was missing its `--rollup` option, even though both are real, tested
  Typer commands — so a schema-driven agent couldn't discover them. A new
  regression test walks the actual Typer command tree and asserts every
  command has a schema entry whose option list matches the real options
  exactly, so this class of drift can't reoccur silently. Closes #84.
- Removed `source.endpoints_called`/`source.cached` from successful tool
  responses — they were never populated (`mcp.tools._source()` had no path
  to set them) and always reported `[]`/`false`, even on network-hitting or
  cache-served calls, so agents could be misled into treating fabricated
  negative values as real signal. A single tool call can span multiple
  independently cached-or-not sub-calls against different upstream
  endpoints, so a flat list/bool is either silently incomplete or ambiguous
  once real — worse than not advertising it. Error-envelope provenance
  (`error.source.endpoints_called`, which records the one endpoint that
  actually failed) is unaffected — that one has no such ambiguity and was
  already populated correctly. Also fixed `cwms_get_overview_section`,
  the one tool whose success responses carried no `source` at all despite
  the module's own "every successful tool response carries
  `source.fingerprint`" contract — all three of its success branches
  (index, section, chunk) now carry it. Closes #70.
- The capability fingerprint now moves when agent-visible selection prose
  changes, not just when a schema does. Previously a tool `description`
  rewrite, its latency class, a resource's `name`/`title`/`description`, the
  FastMCP server `instructions` string, or the capability-summary prose in
  `capabilities_payload()` (what the server does/does not do, error-handling
  and response-shape guidance, deprecation policy) could all change without
  moving the fingerprint, leaving cached agents unaware their selection
  criteria were stale. `mcp.contract.tool_definitions()` now carries each
  tool's live `description`/`title`; a new `mcp.contract.resource_definitions()`
  live-introspects resource/template `name`/`title`/`description` from the
  real FastMCP registration (merging in `error_codes` from the existing
  hand-maintained inventory, since FastMCP has no introspection point for
  those, plus a new test pinning that inventory's URI set against the live
  one so the two can't silently diverge); a new
  `mcp.resources.capability_contract_payload()` factors out the static,
  non-circular subset of the capability summary (including `tool_latency`) as
  its own fingerprint input; and the server's `instructions` string is now
  read from the live built server via `mcp.contract.server_instructions()`
  rather than a separately-referenced constant. Deliberately excluded as
  runtime-volatile rather than source-controlled prose: `prerequisites.api_root`/
  `user_agent`, the `fastmcp` installed-version/drift diagnostics, and
  `active_workarounds` (all already covered, where relevant, by the existing
  version/runtime-baseline fingerprint inputs). Closes #71.
- `cwms_search_places` pagination cursors are unkeyed base64url(JSON), and the
  `req` field is an unkeyed hash of the query/parameter — anyone can
  construct a valid-looking cursor without ever having called the tool.
  Previously the continuation path trusted a forged cursor's embedded office
  list (up to 200 offices) directly, bypassing the per-call uncached-office
  fan-out budget the fresh-request path enforces — a hand-crafted cursor
  could drive up to 200 real upstream calls in one continuation instead of
  the small per-call cap. The cursor's office list is now re-run through the
  same budget check (`_run_fanout`) on every continuation, not just the
  first call: a legitimate multi-page search pays no extra cost (the locked
  offices are normally still cache-hot moments later), while a forged cursor
  naming many never-cached offices — or, rarely, a legitimate cursor whose
  locked offices fell out of cache between pages — is rejected outright as
  `invalid_cursor` (no upstream calls spent) rather than silently searching
  a smaller office set than the cursor promised. Closes #72.
- Task-response models (`SearchPlacesResponse`, `DescribePlaceResponse`,
  `ValueWithContextResponse`, and the rest of the success-branch tier in
  `core/models.py`) now forbid extra fields (`extra="forbid"`), so every
  outputSchema's success branch advertises `additionalProperties: false`
  instead of `true`. Previously an undeclared producer field silently passed
  through unvalidated and unfingerprinted (combined with #71's since-closed
  fingerprint gap, a field could appear or drift with no signal at all).
  Closing the models surfaced fields that were only ever tolerated via the
  old `extra="allow"` hatch and needed to become real, declared fields:
  `ActiveThreshold.level_id`/`.source_workaround` (detail=full only),
  `ValueWithContextResponse.level_lookup_status` (a new `LevelLookupStatus`
  enum; always present, per `core.values.get_value`'s own docstring), and
  `PublishersForParameterResponse`'s `_observed_publishers_by_office`
  diagnostic (detail=full only; needs `alias`+`serialize_by_alias=True`
  since pydantic forbids a literal underscore-prefixed field name). It also
  surfaced a real bug: `cwms_describe_place`'s producer emitted top-level
  `source_workaround`/`upstream_status` keys purely to feed `source.workaround`/
  `source.upstream_status` — never popped, so every response (both MCP and
  CLI) leaked a redundant, undocumented duplicate of that same information;
  now popped on both surfaces. Also completed a previously half-wired
  feature: `cwms_search_places` results now carry the raw upstream location
  DTO under `raw` at `detail=full` (dropped in `summary`) — the shaping
  layer already had this exact stripping logic, but the producer never
  actually included the field, so it was dead code; `cwms_browse_region`
  deliberately still omits it (an existing, unchanged design decision — an
  agent browsing a region doesn't need every per-row DTO). DTO facades
  (`CdaLocation`, `CdaProject`) are a different, currently-unwired tier and
  keep `extra="allow"` by design. Closes #74.
- Removed `idempotentHint: true` from every tool annotation (all 10 tools
  across `mcp/tools.py` and `mcp/server.py`). The MCP spec only assigns
  `idempotentHint`/`destructiveHint` meaning when `readOnlyHint` is false —
  every tool here is read-only, so asserting `idempotentHint: true` claimed
  protocol semantics that don't apply in this branch rather than omitting a
  hint the spec doesn't define here. Closes #75.
- `ghost_office` errors no longer discard the agent's original call intent.
  Previously every ghost-office repair pointed at `cwms_browse_region`
  regardless of which tool actually failed — e.g. `cwms_get_value(office=NWO,
  name=FTPK, parameter=Elev)` told the agent to call
  `cwms_browse_region(office=NWDM)` instead, dropping `name`/`parameter` and
  forcing re-orchestration (browse, re-find the place, re-call `get_value`).
  The repair now retries the SAME failing tool/CLI command with the SAME
  original arguments, only `office` swapped to the NW rollup target
  (`core.offices.ghost_office_repair`, wired at each MCP tool handler and
  CLI command). CLI commands with no `--office` flag (`place
  describe`/`parameters`, `value get`/`history`/`profile` — these take a
  combined `OFFICE/NAME[/PARAMETER]` positional instead) get a repair
  naming the actual CLI invocation (e.g. `cwms-tools value history`) with
  CLI-native argument names (e.g. `begin`/`end`, not the MCP tool's
  `begin_iso`/`end_iso`), not the MCP tool name — a repair combining an
  MCP tool name with CLI-only argument names would be callable on neither
  surface. Consolidated the NW-stub/rollup map, previously triplicated
  across `core.catalog`, `core.locations`, and `mcp.resources`, into a
  single canonical `core.offices` home. Core no longer builds this repair
  itself (it doesn't know which tool/command is calling); a core-level
  `ghost_office` error now carries `repair: null`, unaffected by this
  change. Closes #69.
- Error envelope `field` now names a real, retryable tool parameter or CLI
  flag instead of a producer-internal or synthetic name. `ghost_office`
  errors reported `field: "office_id"` (the CDA-facing name `core.catalog`/
  `core.locations` use internally), but every MCP tool's actual parameter
  is `office`; an agent applying mechanical field-level repair would retry
  with a rejected argument name. Bounding-box validation errors reported
  `field: "bbox"`, which isn't a real argument on any surface — the four
  corners are separate params/flags. `field` is now translated at every
  output boundary (`mcp.tools.stamp_envelope`, `cli.render.emit_error`, and
  the `value get` bulk per-item path) via a small internal→surface name
  map (`core.errors.surface_field_name`); bbox errors now name the first
  missing corner in canonical south/west/north/east order. `place
  describe`/`parameters` and `value get`/`history`/`profile` have no
  `--office` flag (they take a combined `OFFICE/NAME[/PARAMETER]`
  positional instead), so those five commands redirect the same
  `office_id` producer field to their own actual positional argument
  (`spec` for `place describe`/`parameters`; `id_specs`/`id_spec` for
  `value get`/`history`/`profile`) instead of the (nonexistent, for them)
  `office`. Scoped to the `ghost_office`/bbox mismatches this issue
  reported: `value.py`'s pre-existing `_parse_id` malformed-spec-shape
  error (a separate site, shared across all three `value` commands) still
  emits its own established `field: "id"` convention unchanged. Core-level
  tests still assert the internal producer name unaffected. Closes #68.
- `cwms_search_places`/`cwms_browse_region` (and their CLI equivalents) no
  longer set `truncated: true` when a `limit` cap is hit. `truncated` is
  meant for genuinely unrecoverable caps (as `cwms_get_history` already
  handles correctly); search/browse results are always fully pageable via
  `has_more`/`next_cursor`, so reporting `truncated: true` there could
  cause an agent to give up or over-narrow its query instead of paging.
  `truncated` now always reports `false` for these two tools. Documented
  that this is orthogonal to `cwms_search_places`'s pre-existing
  `offices_skipped_for_budget` (a separate signal for scope — as opposed
  to row — incompleteness), so `truncated: false` never implies every
  requested office was searched. Closes #73.
- `cwms_get_history`/`cwms-tools value history` now cap raw points returned
  under the default `rollup='raw'` at `MAX_RAW_HISTORY_POINTS` (5,000). The
  only prior bound was the upstream 300,000-point page cap, so a naive
  default call over a long window on a high-frequency series (e.g. 90 days
  of 15-minute data) could return tens of thousands of rows — a context
  bomb the tool's own docs warned about without preventing it. Points are
  sorted by timestamp before capping (defensive against an out-of-order
  upstream response) so the kept prefix and derived `next_begin` can't skip
  a point. Capped responses set `truncated: true` and a `truncation_hint`
  that either points at `next_begin` (to continue) and `rollup='hourly'/
  'daily'` (for a compact summary of the full window in one call), or, when
  the upstream page cap *also* fired before reaching the requested window
  end, clarifies that `summary`/`buckets` only cover the fetched prefix —
  switching `rollup` does not recover full-window coverage in that case.
  `summary` and `value_count` are otherwise unaffected by the local cap —
  both still reflect the full fetched window, not just the capped `values`.
  Also reworded the upstream-page-cap `truncation_hint` to name the actual
  callable parameter (`begin_iso`) instead of the opaque `next_begin`/
  `--begin`/`--end` phrasing. Closes #66.
- `cwms_get_profile` now sets protocol `isError: true` on failure like every
  other tool — it was the only tool missing the `@iserror_aware` decorator, so
  its `{ok: false, error: {...}}` envelope previously came back as a plain
  (non-error) tool result. Closes #64.
- Every tool's error response now matches its own published `outputSchema`:
  `_error_tool_result` was constructing `structuredContent` directly and
  bypassing FastMCP's automatic `{"result": ...}` wrap that every tool's Union
  return type (`SomeResponse | ErrorRef`) requires, so error payloads didn't
  conform to the schema success payloads do. Closes #64.
- The `cwms://overview/{section_id}` and `.../chunk/{chunk_id}` resource
  read failures now carry the *same* `ErrorEnvelope` shape tool failures use
  in `error.data`, with only the two renames the JSON-RPC carrier requires
  (`code`→`machine_code`, `message`→`human_message`). This is a breaking
  change to that JSON-RPC `error.data` shape: the bespoke `recoverable` flag is
  gone, and `machine_code` is now the generic `not_found` (matching the code
  `cwms_get_overview_section` already returns for the identical failure)
  instead of the resource-only `section_not_found`/`chunk_not_found` strings.
  Agents branching on those specific strings need to switch to `not_found` +
  the `field` value (`section_id` or `chunk_id`). Each `cwms://` resource's
  possible error codes are now also listed alongside its entry in
  `RESOURCE_INVENTORY`/the `cwms://capabilities` `resources` list (mirroring
  `tool_error_codes`) and are part of the capability fingerprint, so this
  change (and any future resource error-contract change) moves the
  fingerprint. Closes #64. The deeper envelope field-shape migration
  (`temporary`, `details`, `repair.next_step`) this repo's own
  `agent-friendly-mcp` skill now mandates is deliberately out of scope here —
  tracked separately in #76.
- `cwms-tools publisher for-parameter` no longer leaks the internal
  `_observed_publishers_by_office` diagnostic field in its default output. The
  CLI now matches the `cwms_publishers_for_parameter` MCP tool: summary mode
  (the default) strips the field, and a new `--detail full` toggle preserves it
  for debugging. Closes #55.
- Numeric values in tool and CLI responses are now rounded to 6 significant
  figures at the serialization boundary, removing the IEEE-754 noise that unit
  conversion injected (e.g. a water temperature surfaced as
  `68.55000000000001 °F` / `20.305555555555557 °C`). CWMS exposes no
  sensor-precision metadata, so consumers could not strip this themselves;
  6 sig figs sits well below any plausible sensor resolution, so no real signal
  is lost. Rounding is significant-figures (not fixed-decimals) so it holds
  across the huge magnitude range of CWMS parameters (temps ~20, flows in
  thousands of cfs, volts ~12). Coordinates (`latitude`/`longitude`, bbox
  bounds) and cost fields are carved out to preserve citation-grade precision.
  Applies uniformly to the MCP and CLI surfaces. Closes #45.
- `cwms_get_overview_section` no longer requires `section_id` — omitted, it
  now returns the same index as the `cwms://overview` resource, so
  resource-blind clients have a real discovery path instead of hitting a
  tool that required a slug only the resource could teach them. Added
  `cwms_list_offices`, the matching tool fallback for `cwms://offices`
  office-code discovery. The `not_found` repair hint on a missing overview
  section is now an actually-callable `{}` args set (was a non-callable
  placeholder string) and the error message enumerates the real section
  slugs — fixed on both the tool and the `cwms://overview/{section_id}`
  resource. The capability summary no longer claims a
  `$defs/ErrorEnvelope` schema path that deployed schemas don't have (they
  inline all definitions). Closes #65.
- `core.offices.list_offices()` (backing the `cwms://offices` resource, the
  CLI `offices` command, and the new `cwms_list_offices` tool) now treats
  cache init/read/write failures as best-effort instead of letting them
  escape as unstructured errors — a broken cache directory or corrupted
  store previously bypassed the documented "degrades to a fallback slice"
  behavior that only guarded the upstream fetch. Closes #78.

## [0.5.0] - 2026-06-16

### Added

- `cwms_search_places` now returns a structured `repair_hint` when a bare-name
  search resolves to no office in scope (the common "I know the place but not
  the USACE office" case). Instead of an empty result with only the generic
  `no_offices_in_scope` reason, the response names a concrete, copy-paste
  retryable office list (the curated data-bearing set: NWDM/NWDP regional
  rollups plus the district-publishing offices) under
  `repair_hint.args.office`, echoing the caller's `query`/`parameter`. The
  bare-name path is no longer a dead end resolved only by guessing office
  codes. (Adds a `repair_hint` field to the response schema, bumping the
  capability fingerprint.) Closes #24.

- `cwms_get_history` now supports server-side time aggregation, cutting the
  token cost of the most common "how has X changed over N days?" question:
  - A `summary` block (first, last, min, max, mean, delta, count over the
    window's non-null observations) — the key is always present (null only when
    the window has no numeric observations) — so agents no longer pull every
    hourly point and hand-compute deltas (a token cost and a correctness risk).
  - A new `rollup` parameter (`raw` (default) | `hourly` | `daily`; CLI:
    `--rollup`) downsamples server-side. `hourly`/`daily` return per-bucket
    `{timestamp, min, max, mean, count}` rows in a new `buckets` field (with an
    empty `values`), bucketed on UTC hour/day boundaries — a handful of rows
    instead of hundreds.
  (Adds `rollup`/`summary`/`buckets` to the response schema and a `--rollup`
  CLI option, bumping the capability fingerprint.) Closes #25.

- `cwms_get_profile` MCP tool + `cwms-tools value profile` CLI command: read an
  entire depth-tagged sensor string in one call instead of one `cwms_get_value`
  per depth. Given a parent string (e.g. `GWLW_S1`) and a parameter, it finds
  the co-located depth sensors that publish it, fetches each one's latest value,
  and returns them sorted shallow→deep with structured `depth: {value, unit}`.
  A single dead sensor degrades to `value: null` + an `error` code rather than
  failing the whole profile; when no sensors match, `note` explains recovery.
  Closes #26.
- Structured sensor depth on search/browse results: `cwms_search_places` and
  `cwms_browse_region` rows now carry a `depth: {value, unit}` field when the
  location id is a depth-tagged WQ sensor (e.g. `GWLW_S1-D3,0ft` →
  `{value: 3.0, unit: "ft"}`, `BECR-D042,5m` → `{value: 42.5, unit: "m"}`),
  removing the guesswork that the comma is a decimal point and that the
  trailing `,0ft` does not mean "0 ft". Closes #27.
  (Adds a tool, a CLI command, and the `depth` response field — bumps the
  capability fingerprint.)
- CLI help parity (docs only, no behavior change): `place search`'s `--office`
  help and docstring now advertise that omitting `--office` on an empty scope
  returns a top-level `repair_hint` (Closes #33), and the `place search` /
  `region browse` docstrings now note that depth-tagged sensor rows carry a
  structured `depth: {value, unit}` (Closes #34). Both fields were already
  emitted; only the CLI help lagged the MCP wording.

### Changed

- Tool failures now set the protocol-level `isError: true` flag in addition to
  carrying the structured `{ok: false, error: {...}}` envelope in
  `structuredContent`. The envelope remains the stable, branchable contract —
  agents should still discriminate on the `ok` field — and `isError` is an
  additive signal for MCP clients that key on it. Implemented by returning
  `ToolResult(is_error=True, structured_content=<envelope>)` from every tool's
  error path. The FastMCP runtime baseline records this under
  `VERIFIED["tool_error_iserror"]` (moved from `FALLBACKS`), which bumps the
  capability fingerprint. Closes #19.

## [0.4.0] - 2026-06-10

### Added

- `cwms://offices` MCP resource: the USACE office directory (name, long name,
  type, reporting parent for all ~68 offices) so agents can discover valid
  `office` parameter values without out-of-band knowledge. Carries the NW
  regional-rollup guidance (query NWDM/NWDP, not the NWO/NWK/NWS/NWP/NWW
  district stubs) and degrades to a documented fallback slice with
  `partial: true` when upstream is unreachable on a cold start. Cached 7 days.
  Every tool's `office` parameter description, the server instructions, and the
  `ghost_office` repair hints now point at it. (Adding the resource bumps the
  capability fingerprint.) Closes #21.

- `cwms-tools offices` CLI command: the CLI analog of the `cwms://offices`
  resource, sharing the same core. Lists the office codes the `--office` option
  expects plus the NW regional-rollup guidance, so CLI agents can discover valid
  codes instead of relying on out-of-band knowledge. Every `--office` help
  string now points at it. (Adding the command bumps the capability fingerprint.)

- Agent-friendly GitHub hardening (no runtime changes):
  - `AGENTS.md` is now the canonical instructions file; `CLAUDE.md` is a thin
    `@AGENTS.md` pointer.
  - `.github/dependabot.yml` for `uv` and `github-actions` weekly updates.
  - `.gitattributes` normalizes line endings (`* text=auto eol=lf`).
  - CI gains a `ci-success` aggregate gate (the single required status check) and a
    PR-only `dependency-review` job.
  - All third-party GitHub Actions are pinned to full commit SHAs.
- MCPB bundle packaging: `manifest.json` generated from `pyproject.toml` and the
  live server by `scripts/gen_manifest.py`, packed into an installable `.mcpb`
  on release (`.github/workflows/mcpb.yml`). Manifest staleness is guarded by a
  prek hook, the CI `verify` job, and `tests/test_manifest.py`.
- CI now tests on Python 3.14, with the macOS/Windows checks bumped to 3.14.

### Fixed

- `cwms_search_places` no longer silently swallows per-office errors: a single
  NW-stub office (NWO/NWK/NWS/NWP/NWW) now returns the `ghost_office` envelope
  with its repair hint, and a failing office in a multi-office fan-out is
  reported via `partial: true` + `partial_reasons`.
- The ghost-office repair hint from single-location reads now points at
  `cwms_browse_region` (was a useless empty-query `cwms_search_places` call).
- Error responses now carry `source.fingerprint`, matching success responses.

### Changed

- Modernized for the Python 3.11 floor: ruff `target-version` raised to `py311`;
  the str-mixin enums (`ErrorCode`, `Detail`, `Unit`, `StatusClass`) converted to
  `enum.StrEnum`; and `datetime.timezone.utc` replaced with `datetime.UTC`. No
  agent-visible output or capability-fingerprint changes.
- Release pipeline hardening: a tag-gated `verify` job fails unless the git tag,
  `pyproject` version, and latest CHANGELOG heading agree; PyPI publish uses
  `skip-existing` for safe reruns; least-privilege top-level `permissions`; and
  the dist upload fails fast on no files.
- Tool `outputSchema`s now document the full error envelope (`code`, `field`,
  `repair`, `retryable`, `retry_after_ms`, `request_id`) instead of an opaque
  `error` object. Wire shape is unchanged.
- Removed dead error codes `session_unconfigured` and `truncated` from the
  error contract; `ghost_location`, `publisher_unavailable`, and `wrapper_bug`
  are now advertised as reserved (planned, not yet emitted) in
  `cwms://capabilities`, which also documents the live/reserved/per-tool
  code-list relationship.
- Responses now omit null-valued fields instead of serializing them; `value`
  and `timestamp` on observations keep explicit nulls (null = no observation).
  The convention is documented in `cwms://capabilities` under `response_shape`.
- `invalid_cursor` errors now echo the offending cursor/context in
  `offending_value`, bounded to 64 characters against forged-token payloads.
- The capability summary now reports the FastMCP verification baseline
  (`verified_against`, `drift`) and declares the deprecation policy; the
  baseline is folded into the capability fingerprint.
- MCP resources have explicit agent-facing names and titles (`capabilities`,
  `overview-index`, `overview-section`, `overview-chunk`) instead of leaked
  Python identifiers.
- Error envelopes carry `protocol_request_id` (the JSON-RPC request id) when
  the runtime exposes it, alongside the server-generated `request_id`.
- The fastmcp dependency floor moved from `>=3` to `>=3.4.2`; the capability
  baseline was re-verified against 3.4.2 (protocol `isError: true` with
  structured content is now possible — migration tracked in issue #19).

### Removed

- Dropped Python 3.10 support; the minimum is now Python 3.11
  (`requires-python = ">=3.11"`). 3.10 reaches end-of-life in October 2026.

## [0.3.0] - 2026-06-06

Agent-friendliness remediation (M-1, M-2, C-1, m-4–m-7): opaque cursor
pagination, history continuation, structured CLI schema, tool annotations,
latency metadata, and CLI contract folded into the capability fingerprint.

### Added

- **Opaque cursor pagination on `cwms_search_places` / `cwms_browse_region`.**
  Both tools now accept an optional `cursor` parameter (CLI: `--cursor`).
  Success responses carry `has_more` and `next_cursor`; when `has_more` is
  `true`, pass `next_cursor` as `cursor` on the next call to continue paging
  without repeating the search. The cursor encodes the office set so the
  fanout is locked across pages.
- **History window-continuation via `next_begin`.** When `cwms_get_history`
  truncates a response at the upstream 300 000-point limit it now sets
  `next_begin` (ISO-8601) in the response, giving the exact timestamp to
  pass as the next `begin_iso` / `--begin` to continue the window without
  overlap or gap.
- **`invalid_cursor` error code** (exit 2). A malformed or mismatched cursor
  returns `error.code = "invalid_cursor"`. Cheap mismatches — an undecodable
  token, a changed query/parameter, a malformed offset, or a cursor combined
  with an unlimited limit — are rejected before any fan-out. Catalog-shift
  staleness (the result-set size changed since the cursor was minted) is
  detected after the result set is assembled, which may have touched the
  network or cache depending on cache state.
- **`openWorldHint` and `idempotentHint` annotations** on all eight MCP tools.
  The seven live-CDA tools declare `openWorldHint: true` and `idempotentHint: true`;
  the bundled local-content tool (`cwms_get_overview_section`) declares `openWorldHint: false`
  so clients know which tools reach external services.
- **Per-tool latency metadata in `cwms://capabilities`** (`tool_latency`).
  Each tool entry lists its expected latency class (`local`, `cached`, `network`,
  `slow`) so agents can budget timeouts before calling.
- **Structured CLI `schema` output.** Each command entry now includes an
  `arguments` list (positional arguments with name, type, required, variadic),
  an `options` list (flags with name, type, default, enum, required,
  repeatable), a per-command `error_codes` list (each `{code, exit}` the
  command can emit), and a `latency_class` field.
- **CLI command contract folded into the capability fingerprint.** The
  fingerprint now covers the full CLI command schema in addition to the MCP
  tool/resource surface, so any change to a command's flags, error codes, or
  latency class moves the fingerprint.
- **Explicit `ok: true` discriminator** on all success responses. Every
  successful task response now carries `ok: true` alongside the existing
  `ok: false` on error envelopes, giving agents a single boolean to branch on
  without inspecting content.

### Changed

- Capability fingerprint value changes (additive surface growth). Agents or
  clients that cached the v0.2.0 fingerprint should re-walk `cwms://capabilities`
  on first call.
- Tool input/output schemas gained fields (`cursor`, `has_more`, `next_cursor`,
  `next_begin`, `ok`). These are all additive; existing callers that ignore
  unknown fields are unaffected.

### Known Limitations

- **m-5 (completion for resource-template variables) deferred.** FastMCP 3.3.1
  exposes no completion hook for URI-template variables; agents discover valid
  `{section_id}` values via the `cwms://overview` index resource. This will be
  revisited when the hook lands upstream.
- The MCP `isError` flag on tool failures remains framework-limited (FastMCP
  cannot set it alongside structured content). The in-band `ok: false`
  discriminator is the documented and tested contract.

## [0.2.0] - 2026-05-20

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
  coverage. The per-call fetch budget is now consumed on *attempt* (not just on
  success), so a run of erroring uncached offices can't exceed the cap.

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
- **A negative `limit` on `cwms_search_places` / `cwms_browse_region` now returns
  a `usage_error` envelope** instead of an unstructured server error (the core's
  `ValueError` was not caught by the tool's `CwmsToolsError`-only handler).

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

[Unreleased]: https://github.com/briandconnelly/cwms-tools/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/briandconnelly/cwms-tools/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/briandconnelly/cwms-tools/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/briandconnelly/cwms-tools/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/briandconnelly/cwms-tools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/briandconnelly/cwms-tools/releases/tag/v0.1.0
