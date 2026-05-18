"""`cwms-tools mcp serve` — launch the FastMCP server over stdio or streamable HTTP.

stdio is the only transport where stdout is reserved for the JSON-RPC stream
(agent-friendly-mcp §1). The subcommand suppresses every Typer / rich / log
write to stdout and routes them to stderr, and installs a `sys.stdout` guard
that errors loudly if anything outside FastMCP writes to stdout during the
serve loop.

streamable HTTP is the network-deployment transport; output rules don't
apply because there's no shared stdout channel with the client.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any

import typer

from cwms_tools.cli.render import diagnostic
from cwms_tools.mcp.server import build_server

app = typer.Typer(
    name="mcp",
    help="Run the cwms-tools MCP server.",
    no_args_is_help=True,
)


class _StdoutGuard:
    """Wraps `sys.stdout` and forbids non-FastMCP writes during stdio serve."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._warned = False

    def write(self, s: str) -> int:
        if not s:
            return 0
        sys.stderr.write(s)
        if not self._warned and s.strip():
            sys.stderr.write(
                "\n[cwms-tools mcp serve] non-FastMCP stdout write redirected to stderr\n"
            )
            self._warned = True
        return len(s)

    def flush(self) -> None:
        sys.stderr.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@app.command("serve")
def serve(
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="MCP transport: stdio | streamable-http",
            case_sensitive=False,
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option("--host", help="HTTP bind host (only used for streamable-http)."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="HTTP bind port (only used for streamable-http)."),
    ] = 8765,
) -> None:
    """Launch the MCP server.

    Examples:
      cwms-tools mcp serve --transport stdio
      cwms-tools mcp serve --transport streamable-http --port 8765
    """
    transport_norm = transport.lower().strip()
    server: Any = build_server()

    if transport_norm in {"stdio", "stdin/stdout"}:
        _serve_stdio(server)
    elif transport_norm in {"http", "streamable-http", "streamable_http"}:
        _serve_http(server, host=host, port=port)
    else:
        diagnostic(f"unknown transport {transport!r}; use stdio or streamable-http.")
        raise typer.Exit(code=2)


def _serve_stdio(server: Any) -> None:
    """Install the stdout guard, route Typer/rich/log to stderr, then run."""
    os.environ.setdefault("NO_COLOR", "1")
    os.environ.setdefault("CLICOLOR", "0")
    os.environ.setdefault("TYPER_DEFAULT_FORCE_TERMINAL_WIDTH", "200")

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)

    sys.stdout = _StdoutGuard(sys.__stdout__)
    try:
        server.run(transport="stdio", show_banner=False)
    finally:
        sys.stdout = sys.__stdout__


def _serve_http(server: Any, *, host: str, port: int) -> None:
    """Run streamable-http transport."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        show_banner=False,
    )
