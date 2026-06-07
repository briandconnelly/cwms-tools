"""Opaque cursor encoding for paginated list tools (search_places, browse_region).

A cursor is a base64url(JSON) token carrying the next offset, a hash of the
normalized request, the locked office set, and the full-result total. On
continuation the producer recomputes the result set over the locked offices
and validates the hash + total; any mismatch (changed query/filter, or a
catalog that shifted under us) raises `invalid_cursor` so the agent restarts
without the cursor rather than silently skipping or duplicating rows.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, cast

from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

CURSOR_VERSION = 1

#: A cursor's locked office set is bounded — a hand-crafted token must not be
#: able to drive an unbounded fan-out. (USACE has ~70 offices; 200 is generous.)
MAX_CURSOR_OFFICES = 200


def request_hash(parts: dict[str, Any]) -> str:
    """Stable, order-independent short hash of the normalized request."""
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise invalid_cursor(f"cursor is not a valid token: {exc}") from exc
    if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION:
        raise invalid_cursor("cursor version is unsupported; restart without a cursor")
    return data


def invalid_cursor(message: str, *, repair: RepairHint | None = None) -> CwmsToolsError:
    return CwmsToolsError.of(
        ErrorCode.INVALID_CURSOR,
        message,
        field="cursor",
        hint="Re-issue the original call without `cursor` to restart pagination.",
        repair=repair,
    )


def validate_continuation(cursor: dict[str, Any], *, kind: str, req: str) -> int:
    """Cheap pre-fan-out checks: kind, request hash, offset shape. Returns offset.

    The `total` check is intentionally NOT here — it needs the assembled result
    set, so callers run `ensure_total` after gathering. This lets a mismatched
    cursor fail before any upstream fan-out.
    """
    if cursor.get("kind") != kind:
        raise invalid_cursor("cursor was issued for a different operation")
    if cursor.get("req") != req:
        raise invalid_cursor("cursor does not match the current query/filters")
    offset = cursor.get("off")
    if not isinstance(offset, int) or offset < 0:
        raise invalid_cursor("cursor offset is malformed")
    return offset


def ensure_total(cursor: dict[str, Any], *, total: int) -> None:
    """Post-assembly check: the full result set must match the cursor's snapshot."""
    if cursor.get("total") != total:
        raise invalid_cursor("result set changed since the cursor was issued (catalog shifted)")


def coerce_offices(cursor: dict[str, Any]) -> list[str]:
    """Validate + return the cursor's locked office set before any fan-out.

    Rejects a malformed `offices` payload (non-list, over-long, or non-string
    members) so a forged cursor cannot widen the search beyond its snapshot.
    """
    offices = cursor.get("offices")
    if (
        not isinstance(offices, list)
        or len(offices) > MAX_CURSOR_OFFICES
        or not all(isinstance(o, str) for o in offices)
    ):
        raise invalid_cursor("cursor office set is malformed")
    return cast("list[str]", list(offices))


__all__ = [
    "CURSOR_VERSION",
    "MAX_CURSOR_OFFICES",
    "coerce_offices",
    "decode_cursor",
    "encode_cursor",
    "ensure_total",
    "invalid_cursor",
    "request_hash",
    "validate_continuation",
]
