"""Version-gated workarounds for known cwms-python bugs.

Each workaround is keyed to an upstream issue and isolated here so it can be
removed cleanly when the bug is confirmed fixed upstream. Stubs only in M2;
full implementations land in M5.
"""

from __future__ import annotations

from typing import Final

#: Map of upstream issue → workaround identifier emitted in `error.source.workaround`.
ACTIVE_WORKAROUNDS: Final[dict[str, str]] = {
    "issue-286": "seasonal_level_as_ts",
    "ftpk_project_format_error": "project_format_error_fallback",
}


def is_active(workaround_id: str) -> bool:
    """Return True if the given workaround is currently enabled."""
    return workaround_id in ACTIVE_WORKAROUNDS.values()


def active_workarounds() -> list[str]:
    """Sorted list of active workaround identifiers. Part of the capability fingerprint."""
    return sorted(ACTIVE_WORKAROUNDS.values())


__all__ = ["ACTIVE_WORKAROUNDS", "active_workarounds", "is_active"]
