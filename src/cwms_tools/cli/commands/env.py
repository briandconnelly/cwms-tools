"""`cwms-tools env` — print the CWMS_TOOLS_* env vars the CLI reads."""

from __future__ import annotations

import os

import typer

from cwms_tools.cli.render import emit

app = typer.Typer(
    name="env", help="Show the CWMS_TOOLS_* env vars the CLI reads and their resolved values."
)

# Single source of truth for which env vars we read. Used both here and by the
# `config show --resolved` command (M3) and (eventually) `cwms-tools schema`.
READ_VARS: tuple[str, ...] = (
    "CWMS_TOOLS_API_ROOT",
    "CWMS_TOOLS_CACHE_DIR",
    "CWMS_TOOLS_WORKERS",
    "CWMS_TOOLS_REPO_URL",
    "CWMS_TOOLS_USER_AGENT_EXTRA",
    "CWMS_TOOLS_OPERATOR_EMAIL",
    "CWMS_TOOLS_MAX_RPS",  # declared, not enforced in v0.1.0
    "CWMS_API_KEY",  # declared, unused in v0.1.0
    "CWMS_TOKEN",  # declared, unused in v0.1.0
)

# Vars whose values must be redacted in output (tail-only preserved).
SECRET_VARS: frozenset[str] = frozenset({"CWMS_API_KEY", "CWMS_TOKEN"})


def _redacted(name: str, value: str) -> str:
    if name not in SECRET_VARS:
        return value
    if len(value) <= 8:
        return "***"
    return f"***{value[-4:]}"


@app.callback(invoke_without_command=True)
def env_cmd() -> None:
    """Emit the env vars we read, their values (redacted where appropriate)."""
    rows: list[dict[str, str | None]] = []
    for name in READ_VARS:
        raw = os.environ.get(name)
        rows.append(
            {
                "name": name,
                "value": _redacted(name, raw) if raw is not None else None,
                "set": str(raw is not None).lower(),
                "secret": str(name in SECRET_VARS).lower(),
            }
        )
    emit({"variables": rows})


__all__ = ["READ_VARS", "SECRET_VARS"]
