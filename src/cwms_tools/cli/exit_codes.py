"""CLI exit code mapping.

Mirror the numeric map from `agent-friendly-cli` §"Errors And Exit Codes" so
the shell-branching fallback is consistent with the symbolic `error.code` that
appears in the JSON payload.
"""

from __future__ import annotations

from cwms_tools.core.errors import ErrorCode, exit_code_for

OK = 0
GENERIC_ERROR = 1
USAGE_ERROR = 2
NOT_FOUND = 3
UNAUTHENTICATED = 4
FORBIDDEN = 5
RATE_LIMITED = 6
TIMEOUT = 7
CONFLICT = 8
TRANSIENT_RETRYABLE = 9
WRAPPER_BUG = 11
GHOST = 12


def from_error_code(code: ErrorCode) -> int:
    """Re-export of `errors.exit_code_for` for CLI-side imports."""
    return exit_code_for(code)


__all__ = [
    "CONFLICT",
    "FORBIDDEN",
    "GENERIC_ERROR",
    "GHOST",
    "NOT_FOUND",
    "OK",
    "RATE_LIMITED",
    "TIMEOUT",
    "TRANSIENT_RETRYABLE",
    "UNAUTHENTICATED",
    "USAGE_ERROR",
    "WRAPPER_BUG",
    "from_error_code",
]
