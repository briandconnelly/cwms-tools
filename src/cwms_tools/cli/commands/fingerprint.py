"""`cwms-tools fingerprint` — emit the capability fingerprint alone."""

from __future__ import annotations

import typer

from cwms_tools.cli.render import emit
from cwms_tools.core import fingerprint as fp
from cwms_tools.mcp.resources import RESOURCE_INVENTORY, TOOL_INVENTORY

app = typer.Typer(
    name="fingerprint",
    help="Emit the capability fingerprint for cheap cache-validity checks.",
)


@app.callback(invoke_without_command=True)
def fingerprint_cmd() -> None:
    """Compute and emit the capability fingerprint."""
    # We pass the tool inventory as a stable list of names (no per-tool schema
    # introspection on the CLI side) — that matches what the MCP server sees
    # because the tool surface in v0.1.0 is statically registered.
    tools = {name: {"name": name} for name in TOOL_INVENTORY}
    digest = fp.compute(tools=tools, resources=RESOURCE_INVENTORY)
    emit(
        {
            "fingerprint": digest,
            "scope": fp.FINGERPRINT_SCOPE,
        }
    )
