# cwms-tools

Agent-friendly tools for the U.S. Army Corps of Engineers' [CWMS Data
API][cda]. Provides an [MCP][mcp] server and a CLI on top of the
official [`cwms-python`][cwms-python] client, designed so AI agents
(Claude Code, Codex, custom) can answer hydrologic questions in **one
tool call instead of five**.

## Status

**v0.1.0 — pre-PyPI, read-only, alpha.** See [CHANGELOG.md](CHANGELOG.md).

## What it does / doesn't do

**Does** (v0.1.0)

- Resolve place names to canonical CWMS locations, with ghost
  filtering and co-located variant detection.
- Fetch the latest value or windowed history for any parameter,
  inline-classified against the applicable thresholds.
- Browse the catalog by office, region, or bounding box.
- Surface the `cwms-overview.md` knowledge as a queryable MCP
  resource — agents don't have to pre-load it.

**Does not** (v0.1.0)

- Write / store / delete anything.
- Forecast retrieval (deferred to v0.2).
- USGS, NOAA, or any non-CWMS data sources.
- DSS or XML file decoding.

## What CWMS is

CWMS — the Corps Water Management System — is USACE's platform for
operating and reporting on the federal water resources it manages:
mainstem reservoirs, hydropower projects, flood-control dams,
navigation locks, and environmental monitoring stations. The
[`cwms-overview.md`](cwms-overview.md) file in this repository is a
self-contained orientation. This package **wraps the existing public
API**; it does not replace `cwms-python`.

## Install

> Until the package lands on PyPI, install from source:

```bash
git clone https://github.com/bdc/cwms-tools.git
cd cwms-tools
uv sync
```

Once on PyPI:

```bash
uv add cwms-tools
```

Python 3.10+. No authentication required for v0.1.0 (read endpoints
on CWMS Data API are public).

## CLI quick-start

```bash
# What does this install think it is, and what does it know how to do?
$ uv run cwms-tools whoami
{
  "identity": "anonymous",
  "api_root": "https://cwms-data.usace.army.mil/cwms-data/",
  "user_agent": "cwms-tools/0.1.0 (+https://github.com/bdc/cwms-tools) cwms-python/1.0.7",
  "operator_email": null
}

$ uv run cwms-tools fingerprint
{
  "fingerprint": "2a627f55864d017fe2dfaad4e0aebd8baac9e551046c6ef35a4cebdf054bb488",
  "scope": "schema-contract"
}

# Resolve a place name -> ranked, ghost-filtered location matches
$ uv run cwms-tools place search "Fort Peck" --office NWDM

# Latest value at a place + inline status classification (one tool call)
$ uv run cwms-tools value get NWDM/FTPK/Elev

# Describe a project: location + project metadata + publisher fingerprint
$ uv run cwms-tools place describe NWDM/FTPK

# Catalog browse with bbox or state filter
$ uv run cwms-tools region browse --office SWT --state OK

# Which publishers report on a parameter, across cached offices
$ uv run cwms-tools publisher for-parameter Elev --office NWDM --office SWT

# Windowed history with summary or full detail
$ uv run cwms-tools value history NWDM/FTPK/Elev \
    --begin 2026-05-16T00:00:00Z --end 2026-05-17T00:00:00Z
```

Top-level flags: `--machine` (compact JSON, auto-enabled on non-TTY),
`--json` (alias), `--no-cache`, `--isolated`, `--version`.

Exit codes follow `agent-friendly-cli`: `2` usage, `3` not_found,
`6` rate_limited, `7` timeout, `11` wrapper_bug, `12` ghost.
Every command emits structured JSON on stdout; diagnostics go to
stderr.

## MCP quick-start

```bash
# stdio (local agent runtime)
uv run cwms-tools mcp serve --transport stdio

# streamable HTTP (remote / shared deployment)
uv run cwms-tools mcp serve --transport streamable-http --port 8765
```

Claude Code config snippet:

```jsonc
// ~/.claude/mcp.json
{
  "mcpServers": {
    "cwms-tools": {
      "command": "uv",
      "args": ["run", "cwms-tools", "mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

## Discovery and gotchas

Agents that have already loaded the server can browse the bundled
overview content without re-fetching it from the network:

- `cwms://capabilities` — what the server does, version, fingerprint.
- `cwms://overview/{section_id}` — sections of `cwms-overview.md`,
  with a `?detail=summary|full` toggle and chunked bodies for large
  sections.

Two recurring traps that the package handles for you but are worth
knowing exist:

- **Ghost records.** Many CWMS catalog entries carry no time-series
  data. `cwms-tools` filters them out by default; explicit error
  payloads carry a `repair` field pointing at the next call to make.
- **NW District stubs.** `NWO`, `NWK`, `NWS`, `NWP`, `NWW` are
  near-empty stubs in CDA; use `NWDM` (Missouri) or `NWDP` (Pacific
  NW) instead. Calls that target a stub office are auto-rewritten in
  error envelopes.

## Etiquette / reporting issues

This package treats the CWMS Data API as a shared public resource.
We identify ourselves with a descriptive `User-Agent`, cap concurrent
requests, honor `Retry-After` headers, and never run background
catalog scans. (`CWMS_TOOLS_MAX_RPS` is declared in the schema but
not enforced in v0.1.0.) If you operate the upstream service and
this client is misbehaving, please open an issue at
<https://github.com/bdc/cwms-tools/issues> and we will ship a point
release that the cache namespace key invalidates against.

## Development

```bash
uv sync                                  # set up dev environment
uv run prek run --all-files              # ruff, ty, pytest-fast
uv run pytest --cov=cwms_tools           # full test suite
uv run ty check                          # type check
```

CONTRIBUTING.md is deferred to v0.2; until then, please open an
issue before sending non-trivial PRs.

## License

[MIT](LICENSE). Matches the license of the upstream
[`cwms-python`][cwms-python].

[cda]: https://cwms-data.usace.army.mil/cwms-data/
[mcp]: https://modelcontextprotocol.io/
[cwms-python]: https://github.com/HydrologicEngineeringCenter/cwms-python
