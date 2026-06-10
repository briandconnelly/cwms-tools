"""`cwms-tools offices` — list USACE office codes for the `--office` option.

The CLI analog of the `cwms://offices` MCP resource: the office codes every
CDA-hitting command's `--office` expects are otherwise out-of-band knowledge.
Reuses the same `offices_payload()` core, so the two surfaces stay in lockstep.
"""

from __future__ import annotations

import typer

from cwms_tools.cli.render import emit
from cwms_tools.mcp.resources import offices_payload

app = typer.Typer(
    name="offices",
    help=(
        "List USACE office codes (the values `--office` expects), with the "
        "NW regional-rollup guidance — query NWDM/NWDP, not the NW district "
        "stubs. Network-backed and cached 7 days."
    ),
)


@app.callback(invoke_without_command=True)
def offices() -> None:
    """Emit the USACE office directory plus NW regional-rollup guidance."""
    emit(offices_payload())
