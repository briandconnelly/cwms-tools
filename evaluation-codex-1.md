# cwms-tools CLI Evaluation - Codex 1

I exercised `uv run cwms-tools` across help/schema, place search, parameters,
describe, value/history, env/config/whoami, region ghost-office handling, and
publisher lookup.

## University Bridge Water Temperature

Current University Bridge water temperature found during evaluation:

- Value: `14.6667 C` (`58.4 F`)
- Timestamp: `2026-05-18T23:00:00Z`
- Time series: `UBLW_S1-D21,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV`
- Sensor/location id: `NWDP/UBLW_S1-D21,0ft/Temp-Water`

Command that returned the value:

```sh
uv run cwms-tools value history NWDP/UBLW_S1-D21,0ft/Temp-Water --begin 2026-05-18T18:00:00Z --end 2026-05-18T23:30:00Z --unit SI
```

Note: this is the `D21,0ft` sensor in the University Bridge Lake Washington
temperature string. The site appears to have multiple depth-specific sensors.

## Bugs / Risks

### Broad place search can expose a traceback

`place search "Bridge" --office NWDP` and `place search "Temp String" --office
NWDP` hit CDA 500s and printed a Rich traceback plus upstream URL instead of a
structured error envelope.

Likely root cause: `src/cwms_tools/core/catalog.py` builds one large alternation
regex from all matched location names before calling the time-series catalog.
That request can become too large or complex for CDA. The upstream `ApiError` is
not wrapped into `CwmsToolsError`, so the CLI adapter does not emit the normal
structured failure shape.

### `place describe` fails on valid non-project sites

Both of these failed:

```sh
uv run cwms-tools place describe NWDP/UBLW
uv run cwms-tools place describe NWDP/UBLW_S1-D21,0ft
```

The failure was an `upstream_error` from project lookup. Search and parameters
show these are valid locations, but `describe` always calls project lookup and
does not degrade to a partial response for non-project sites. The docstring says
recoverable subcall failures should produce `partial: true`.

### `value get` hung while `value history` worked

This command hung until killed:

```sh
uv run cwms-tools value get NWDP/UBLW_S1-D21,0ft/Temp-Water --unit SI
```

The equivalent `value history` call returned quickly. A direct probe of the known
time series also returned quickly, which points away from the observation fetch.

Likely root cause: `value get` automatically performs threshold classification.
That calls `levels.list_levels(...)`, and a direct probe of the same level-listing
path also hung. There is no CLI flag to skip classification, so agents cannot
fall back to "just give me the current value" when levels lookup is slow or
unhealthy.

## Discoverability / Tool-Use Notes

- `place search` requiring `--office` is clear in help, but there is no first-step
  command for "search all offices" or "which office owns this place?" The
  ghost-office repair hint for `NWS -> NWDP` is useful.
- The University Bridge path is hard to discover. `place search "University
  Bridge"` finds `UBLW` and co-located `UBLW_S1`, but `place parameters
  NWDP/UBLW_S1` returns zero parameters. The actual usable locations are
  depth-suffixed, for example `UBLW_S1-D21,0ft`.
- Search results could expose "related data-bearing depth locations" or a repair
  hint when a co-located parent/string location has no parameters.
- `value get --help` should include an example with commas/depth suffixes, such
  as `NWDP/UBLW_S1-D21,0ft/Temp-Water`, since that id shape is not obvious.
- Consider `value get --no-classify` or `--status none|levels` so agents can
  retrieve a current value when levels lookup is slow or unhealthy.
