"""`cwms-tools whoami` — auth identity (always `anonymous` in v0.1.0)."""

from __future__ import annotations

import typer

from cwms_tools.cli.render import emit
from cwms_tools.core.session import current_config

app = typer.Typer(name="whoami", help="Print the current auth identity (anonymous in v0.1.0).")


@app.callback(invoke_without_command=True)
def whoami() -> None:
    """Emit the resolved auth identity and minimum configuration to disambiguate it."""
    cfg = current_config()
    emit(
        {
            "identity": "anonymous",
            "api_root": cfg.api_root,
            "user_agent": cfg.user_agent,
            "operator_email": cfg.operator_email,
        }
    )
