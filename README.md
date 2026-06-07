# cwms-tools

[![CI](https://github.com/briandconnelly/cwms-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/briandconnelly/cwms-tools/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cwms-tools.svg)](https://pypi.org/project/cwms-tools/)
[![Python](https://img.shields.io/pypi/pyversions/cwms-tools.svg)](https://pypi.org/project/cwms-tools/)

Read-only, agent-friendly tools for the U.S. Army Corps of Engineers'
[CWMS Data API](https://cwms-data.usace.army.mil/cwms-data/). `cwms-tools` wraps the official
[`cwms-python`](https://github.com/HydrologicEngineeringCenter/cwms-python) client with two surfaces that share one behavioral
core:

- an [MCP](https://modelcontextprotocol.io/) server for agent runtimes such as Claude Code, Codex, and custom
  MCP clients
- a non-interactive CLI with compact JSON output, stable exit codes, and a
  machine-readable schema

The goal is simple: let agents answer common hydrologic questions with one task
call instead of a brittle chain of raw API lookups.

## What It Does

- Resolves natural place names to canonical CWMS locations, with ghost-location
  filtering and co-located sensor hints.
- Describes a place in one call: location record, project metadata when
  available, published parameters, publishers, and latest data timestamp.
- Reads the latest value or bounded history for CWMS time series parameters.
- Optionally classifies the latest value against CWMS Location Levels when
  callers opt in with `--with-status` or `with_status=true`.
- Browses one office's catalog by state or bounding box.
- Finds publishers for a parameter across cached or explicitly requested
  offices.
- Serves a bundled CWMS orientation document as MCP resources so agents can load
  background material selectively.

## Install

```bash
uv add cwms-tools
# or:  pipx install cwms-tools
```

To run from a checkout instead:

```bash
git clone https://github.com/briandconnelly/cwms-tools.git
cd cwms-tools
uv sync
uv run cwms-tools --help
```

## CLI Quick Start

The CLI is designed for non-interactive callers. When stdout is not a TTY,
machine mode is enabled automatically: compact JSON on stdout, diagnostics on
stderr, no color, no prompts, and no progress UI. You can force that profile
with `--machine` or `--json`.

```bash
# Inspect the resolved session and upstream API root.
uv run cwms-tools whoami

# Print the command tree, output classes, exit codes, environment inputs,
# MCP tools, and MCP resources as a stable machine-readable contract.
uv run cwms-tools schema

# Print a SHA-256 fingerprint over the agent-visible capability surface.
uv run cwms-tools fingerprint

# Resolve a place name to ranked CWMS locations.
uv run cwms-tools place search "Fort Peck" --office NWDM

# Describe one place: location, project metadata, parameters, publishers,
# freshness, and partial-result flags.
uv run cwms-tools place describe NWDM/FTPK

# List parameters published at a place.
uv run cwms-tools place parameters NWDM/FTPK

# Read the latest elevation value. Status lookup is skipped by default because
# CWMS Location Levels calls can be slow.
uv run cwms-tools value get NWDM/FTPK/Elev

# Opt in to threshold classification when status context matters.
uv run cwms-tools value get NWDM/FTPK/Elev --with-status

# Read a bounded history window.
uv run cwms-tools value history NWDM/FTPK/Elev \
  --begin 2026-05-16T00:00:00Z \
  --end 2026-05-17T00:00:00Z

# Browse an office catalog by state.
uv run cwms-tools region browse --office SWT --state OK

# Page through a large browse result. Pass the prior response's `next_cursor`
# value as --cursor to continue without re-issuing the same search.
uv run cwms-tools region browse --office NWDM --cursor <next_cursor>

# Find publishers reporting a parameter in selected offices.
uv run cwms-tools publisher for-parameter Elev --office NWDM --office SWT
```

Pagination works the same way for `place search`:

```bash
RESULT=$(uv run cwms-tools --machine place search "Reservoir" --office NWDM)
CURSOR=$(echo "$RESULT" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('next_cursor',''))")
# If CURSOR is non-empty, there are more pages:
uv run cwms-tools --machine place search "Reservoir" --office NWDM --cursor "$CURSOR"
```

`value history` sets `next_begin` in the response when the upstream truncates
at 300 000 points. Pass that value as `--begin` on the next call to continue
the window without overlap or gap:

```bash
uv run cwms-tools --machine value history NWDM/FTPK/Elev \
  --begin 2025-01-01T00:00:00Z --end 2025-12-31T23:59:59Z
# If response.next_begin is set, continue from there:
uv run cwms-tools --machine value history NWDM/FTPK/Elev \
  --begin <next_begin> --end 2025-12-31T23:59:59Z
```

Useful global flags:

- `--machine` / `--json`: compact structured output for agents and scripts.
- `--no-cache`: bypass the on-disk catalog cache for one invocation.
- `--isolated`: bypass cache and ignore `CWMS_TOOLS_*` environment variables.
- `--version`: print the installed `cwms-tools` version.

Exit codes are part of the CLI contract:

| Exit | Meaning |
| ---: | --- |
| `0` | success |
| `2` | usage, invalid field, or invalid cursor (`invalid_cursor`) |
| `3` | not found or publisher unavailable |
| `4` | session unconfigured |
| `6` | rate limited |
| `9` | upstream error |
| `11` | wrapper bug |
| `12` | ghost location or ghost office |

## MCP Quick Start

Use stdio for local agent runtimes:

```bash
uv run cwms-tools mcp serve --transport stdio
```

Use streamable HTTP for shared or remote deployment:

```bash
uv run cwms-tools mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

Claude Code config example:

```jsonc
{
  "mcpServers": {
    "cwms-tools": {
      "command": "uv",
      "args": ["run", "cwms-tools", "mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

The MCP server exposes task-level tools rather than raw endpoint mirrors:

| Tool | Purpose |
| --- | --- |
| `cwms_search_places` | Resolve an ambiguous place name to ranked locations. |
| `cwms_describe_place` | Read location, project, parameter, publisher, and freshness data in one call. |
| `cwms_list_parameters` | List parameters published at a location, grouped by publisher. |
| `cwms_get_value` | Read the latest observation, optionally with threshold status. |
| `cwms_get_history` | Read raw observations across a bounded time window. |
| `cwms_browse_region` | Browse one office's locations, optionally by state or bounding box. |
| `cwms_publishers_for_parameter` | List publishers reporting a parameter across selected offices. |
| `cwms_get_overview_section` | Read bundled CWMS orientation content. |

Resources:

- `cwms://capabilities`: server version, fingerprint, tools, and resources.
- `cwms://overview`: index of bundled CWMS overview sections.
- `cwms://overview/{section_id}{?detail}`: summary or full section body.
- `cwms://overview/{section_id}/chunk/{chunk_id}`: one large-section chunk.

## Configuration

`cwms-tools` works anonymously by default. Environment variables are optional:

| Variable | Purpose |
| --- | --- |
| `CWMS_TOOLS_API_ROOT` | Override the CWMS Data API root. |
| `CWMS_TOOLS_CACHE_DIR` | Override the disk cache location. |
| `CWMS_TOOLS_WORKERS` | Set bounded worker concurrency. |
| `CWMS_TOOLS_REPO_URL` | Override the repository URL advertised in the user agent. |
| `CWMS_TOOLS_USER_AGENT_EXTRA` | Append extra text to the user agent. |
| `CWMS_TOOLS_OPERATOR_EMAIL` | Send a contact email via the `From` header. |
| `CWMS_TOOLS_MAX_RPS` | Declared for rate-limit policy; not enforced in v0.1.0. |
| `CWMS_API_KEY` | Reserved secret input for authenticated CDA deployments. |
| `CWMS_TOKEN` | Reserved secret input for authenticated CDA deployments. |

Inspect the resolved configuration without making a data call:

```bash
uv run cwms-tools env
uv run cwms-tools config show --resolved
```

## CWMS Notes

CWMS is USACE's Corps Water Management System: the operational data platform for
federal reservoirs, flood-control dams, navigation locks, hydropower projects,
and environmental monitoring stations.

Two upstream data-shape issues come up often:

- **Ghost records.** Some catalog locations do not publish time-series data.
  Search and browse responses expose `parameter_count`, `parameters`, and
  `data_at` repair hints so agents can move to the data-bearing sibling.
- **Northwestern Division stubs.** `NWO`, `NWK`, `NWS`, `NWP`, and `NWW` are
  near-empty CDA stubs. Use `NWDM` for Missouri River data and `NWDP` for
  Pacific Northwest data. Error envelopes include repair hints when a stub is
  targeted.

## Upstream Etiquette

The CWMS Data API is a shared public service. `cwms-tools` identifies itself
with a descriptive `User-Agent`, caps concurrent requests, avoids background
scans, and does not cache live time-series values. The underlying
`cwms-python` client retries throttled requests with backoff; a `429` that
still surfaces is returned as a `rate_limited` error whose `retry_after_ms`
echoes the upstream `Retry-After` header so callers can back off. If you
operate CDA and see problematic traffic from this client, please open an issue
at <https://github.com/briandconnelly/cwms-tools/issues>.

## Development

```bash
uv sync
uv run prek run --all-files
uv run pytest --cov=cwms_tools
uv run ty check
```

The test suite uses unit tests and mocked CDA responses. Live CDA integration
tests are marked `integration` and are skipped unless explicitly selected.

Before opening a substantial PR, please open an issue to discuss the intended
change. The package is still pre-release, and the CLI/MCP schema contract is
the main compatibility surface.

## License

[MIT](LICENSE)
