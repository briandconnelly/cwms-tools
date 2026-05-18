"""`cwms-tools whoami` — print the resolved CWMS auth identity."""

from __future__ import annotations

import typer

from cwms_tools.cli.render import emit
from cwms_tools.core.session import current_config

app = typer.Typer(
    name="whoami",
    help=(
        "Print the resolved CWMS auth identity. The CWMS Data API's read "
        "endpoints are public, so the identity is `anonymous` unless a "
        "future release adds authenticated write paths."
    ),
)


@app.callback(invoke_without_command=True)
def whoami() -> None:
    """Emit the resolved auth identity and the session config that produced it."""
    cfg = current_config()
    emit(
        {
            "identity": "anonymous",
            "api_root": cfg.api_root,
            "user_agent": cfg.user_agent,
            "operator_email": cfg.operator_email,
        }
    )
