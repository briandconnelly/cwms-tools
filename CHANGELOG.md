# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

Initial public release. Agent-friendly read-only tools for the USACE
[CWMS Data API](https://cwms-data.usace.army.mil/cwms-data/), exposed
as both a [FastMCP 3](https://gofastmcp.com/) server and a
[Typer](https://typer.tiangolo.com/) CLI over one behavioral core.

### Added

- **MCP server** (`cwms-tools mcp serve --transport stdio|streamable-http`)
  with eight tools and six resources:
  - Tools: `cwms_search_places`, `cwms_describe_place`,
    `cwms_list_parameters`, `cwms_browse_region`, `cwms_get_value`,
    `cwms_get_history`, `cwms_publishers_for_parameter`,
    `cwms_get_overview_section`. All declared `readOnlyHint: true` with
    `outputSchema` derived from pydantic v2 models.
  - Resources: `cwms://capabilities`, `cwms://overview` (index),
    `cwms://overview/{section_id}{?detail}` (RFC 6570 query-param
    template), `cwms://overview/{section_id}/chunk/{chunk_id}`,
    `cwms://offices`, `cwms://parameters`.
  - Every tool accepts a `detail: summary | full` toggle that changes
    response density (not shape).
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
- **Test suite**: 193 tests (unit + mocked integration via
  `responses` against the `requests`-backed cwms-python) and a
  fingerprint snapshot suite that pins the v0.1.0 tool/resource/
  error-code surface.

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

[0.1.0]: https://github.com/bdc/cwms-tools/releases/tag/v0.1.0
