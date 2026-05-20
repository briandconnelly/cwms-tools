"""`cwms-tools fingerprint` — emit the capability fingerprint alone."""

from __future__ import annotations

import typer

from cwms_tools.cli.render import emit
from cwms_tools.core import fingerprint as fp
from cwms_tools.mcp.contract import canonical_fingerprint

app = typer.Typer(
    name="fingerprint",
    help=(
        "Print the capability fingerprint — a SHA-256 over the tool list, "
        "schemas, resource catalog, error codes, bundled overview, and "
        "configured CDA root. Clients cache by this value to detect when "
        "anything in the agent-visible surface has changed."
    ),
)


@app.callback(invoke_without_command=True)
def fingerprint_cmd() -> None:
    """Compute and print the capability fingerprint.

    Routes through the same `canonical_fingerprint()` the MCP
    `cwms://capabilities` resource and every tool response use, so all three
    surfaces report an identical value an agent can cache against.
    """
    emit(
        {
            "fingerprint": canonical_fingerprint(),
            "scope": fp.FINGERPRINT_SCOPE,
        }
    )
