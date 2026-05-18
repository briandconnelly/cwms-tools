"""`cwms-tools config show --resolved` — emit effective config after precedence merge."""

from __future__ import annotations

from typing import Annotated

import typer

from cwms_tools.cli.commands.env import READ_VARS, SECRET_VARS
from cwms_tools.cli.render import emit
from cwms_tools.core.cache import resolve_cache_dir
from cwms_tools.core.concurrency import MAX_WORKERS
from cwms_tools.core.session import resolve_session_config

app = typer.Typer(
    name="config",
    help=(
        "Inspect the cwms-tools configuration after the flag > env > default "
        "precedence merge has been applied."
    ),
)


def _redacted(name: str, value: str | None) -> str | None:
    if value is None or name not in SECRET_VARS:
        return value
    return f"***{value[-4:]}" if len(value) > 8 else "***"


@app.command("show")
def show(
    resolved: Annotated[
        bool,
        typer.Option(
            "--resolved",
            help=(
                "Show the merged, effective configuration. Required for forward "
                "compatibility with a `--raw` mode that may later dump on-disk "
                "config files."
            ),
        ),
    ] = False,
) -> None:
    """Print the resolved CLI configuration.

    Precedence: explicit flags > CWMS_TOOLS_* environment variables >
    built-in defaults. `--resolved` is the only mode this release
    accepts; it is required so a future `--raw` mode can be added
    without changing this command's contract.
    """
    if not resolved:
        emit(
            {
                "error": "usage_error",
                "message": "Run `cwms-tools config show --resolved`.",
                "hint": (
                    "This release only supports the --resolved view; --raw is "
                    "reserved for a future addition that dumps on-disk config files."
                ),
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
