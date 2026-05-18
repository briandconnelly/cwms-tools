"""Session configuration over `cwms-python`.

Wraps `cwms.api.init_session` with our own defaults:

- descriptive `User-Agent` and optional `From:` header so USACE operators can
  attribute traffic;
- right-sized connection pool (`max(2 * MAX_WORKERS, 16)`) so we don't advertise
  more concurrency than `core/concurrency.py` will actually use;
- normalized API root (env override + trailing slash).

The session is a process-global singleton inside `cwms-python`; we expose a
fingerprint-friendly summary via `session_fingerprint()` so the capability
fingerprint can include it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

import cwms

from cwms_tools.core.concurrency import MAX_WORKERS

if TYPE_CHECKING:
    from requests import Session

DEFAULT_API_ROOT = "https://cwms-data.usace.army.mil/cwms-data/"
DEFAULT_REPO_URL = "https://github.com/briandconnelly/cwms-tools"


def _cwms_tools_version() -> str:
    try:
        return version("cwms-tools")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


def _cwms_python_version() -> str:
    try:
        return version("cwms-python")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def build_user_agent() -> str:
    """Construct the descriptive User-Agent string.

    Format: `cwms-tools/<v> (+<repo>) cwms-python/<v>[ <extra>]`. The
    `CWMS_TOOLS_USER_AGENT_EXTRA` env var appends a per-deployment token (e.g.
    org name) so forks can be distinguished from the public package.
    """
    repo = os.environ.get("CWMS_TOOLS_REPO_URL", DEFAULT_REPO_URL)
    base = f"cwms-tools/{_cwms_tools_version()} (+{repo}) cwms-python/{_cwms_python_version()}"
    extra = os.environ.get("CWMS_TOOLS_USER_AGENT_EXTRA")
    return f"{base} {extra}".strip() if extra else base


@dataclass(frozen=True)
class SessionConfig:
    """Resolved session configuration. Inputs to the capability fingerprint."""

    api_root: str
    user_agent: str
    operator_email: str | None
    pool_connections: int


def resolve_session_config() -> SessionConfig:
    """Resolve session config from env vars + defaults; deterministic.

    Honors `_CWMS_TOOLS_ISOLATED=1` (set by `cwms-tools --isolated`) by
    ignoring every `CWMS_TOOLS_*` env input and falling back to the defaults.
    Useful for reproducibility checks and CI runs that shouldn't pick up an
    operator's shell config.
    """
    isolated = os.environ.get("_CWMS_TOOLS_ISOLATED") == "1"
    if isolated:
        return SessionConfig(
            api_root=DEFAULT_API_ROOT,
            user_agent=f"cwms-tools/{_cwms_tools_version()} (+{DEFAULT_REPO_URL}) "
            f"cwms-python/{_cwms_python_version()}",
            operator_email=None,
            pool_connections=max(2 * MAX_WORKERS, 16),
        )
    api_root = os.environ.get("CWMS_TOOLS_API_ROOT", DEFAULT_API_ROOT)
    if not api_root.endswith("/"):
        api_root = api_root + "/"
    return SessionConfig(
        api_root=api_root,
        user_agent=build_user_agent(),
        operator_email=os.environ.get("CWMS_TOOLS_OPERATOR_EMAIL"),
        pool_connections=max(2 * MAX_WORKERS, 16),
    )


_state: dict[str, SessionConfig | None] = {"config": None}


def configure_session(config: SessionConfig | None = None) -> SessionConfig:
    """Initialize the cwms-python session with our defaults. Idempotent."""
    resolved = config if config is not None else resolve_session_config()
    session: Session = cwms.init_session(
        api_root=resolved.api_root,
        pool_connections=resolved.pool_connections,
    )
    session.headers["User-Agent"] = resolved.user_agent
    if resolved.operator_email:
        session.headers["From"] = resolved.operator_email
    _state["config"] = resolved
    return resolved


def current_config() -> SessionConfig:
    """Return the currently-configured session config, initializing if needed."""
    cfg = _state["config"]
    if cfg is None:
        return configure_session()
    return cfg


def session_fingerprint() -> dict[str, object]:
    """Stable dict of session inputs that feed the capability fingerprint."""
    cfg = current_config()
    return {
        "api_root": cfg.api_root,
        "user_agent": cfg.user_agent,
        "pool_connections": cfg.pool_connections,
        "has_operator_email": cfg.operator_email is not None,
    }


__all__ = [
    "DEFAULT_API_ROOT",
    "SessionConfig",
    "build_user_agent",
    "configure_session",
    "current_config",
    "resolve_session_config",
    "session_fingerprint",
]
