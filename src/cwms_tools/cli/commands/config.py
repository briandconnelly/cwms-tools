"""`cwms-tools config show --resolved` — emit effective config after precedence merge."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.commands.env import READ_VARS, SECRET_VARS
from cwms_tools.cli.render import emit
from cwms_tools.core.cache import resolve_cache_dir
from cwms_tools.core.concurrency import MAX_WORKERS
from cwms_tools.core.session import resolve_session_config

app = typer.Typer(name="config", help="Inspect resolved CLI configuration.")


def _redacted(name: str, value: str | None) -> str | None:
    if value is None or name not in SECRET_VARS:
        return value
    return f"***{value[-4:]}" if len(value) > 8 else "***"


@app.command("show")
def show(
    resolved: Annotated[
        bool,
        typer.Option("--resolved", help="Show the merged, effective configuration."),
    ] = False,
) -> None:
    """Show the resolved CLI config (flags > env > defaults).

    In v0.1.0 there is no config file, so `--resolved` is the only useful
    mode. The flag is required so future versions can add `--raw` to dump
    on-disk config files without changing this command's contract.
    """
    if not resolved:
        emit(
            {
                "error": "usage_error",
                "message": "Run `cwms-tools config show --resolved`.",
                "hint": "v0.1.0 only supports the --resolved view; --raw is a v0.2 addition.",
            }
        )
        raise typer.Exit(code=2)

    cfg = resolve_session_config()
    payload = {
        "api_root": cfg.api_root,
        "cache_dir": str(resolve_cache_dir()),
        "workers": MAX_WORKERS,
        "user_agent": cfg.user_agent,
        "operator_email": cfg.operator_email,
        "env_inputs_read": list(READ_VARS),
    }
    # Redact any secret env vars surfaced inline (none today, but defensive).
    for k in SECRET_VARS & set(payload.keys()):
        raw = payload[k]
        payload[k] = _redacted(k, raw if isinstance(raw, str) else None)
    emit(payload)
