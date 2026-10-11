"""Navigation-lock records for `describe_place(include_locks=True)` (#134).

cwms-python 1.1.0 added `cwms.locks`. Two upstream quirks shape this module
(observed 2026-10-11, recorded in `tests/fixtures/swt_webb_lock*.json`):

- The list endpoint (`get_locks(office, project_id)`) returns every dimension
  as a `0.0` placeholder with no units, so it is only used to enumerate lock
  names; dimensions come from one `get_lock(name)` per lock.
- Unset dimensions can arrive as CDA's -9999 m sentinel, unit-converted
  (SWT/WEBB `maximum-lock-lift` = -32805.118 ft). Physical dimensions are
  never negative, so a negative value in `LOCK_DIMENSION_FIELDS` is nulled
  and its field name recorded in the lock's `invalid_fields`. Zeros are kept.

A lock lookup never fails the describe: failures degrade into
`lock_lookup_status` (`ok` | `partial` | `failed` | `not_applicable`), a
per-lock `error` code, and `lock_upstream_status` when an HTTP status is
known. Successful upstream reads are cached; failures — including a 200
whose body isn't a lock payload — are not.
"""

from __future__ import annotations

from typing import Any

import cwms.locks.locks as locks_api
from cwms.api import ApiError

from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.session import current_config
from cwms_tools.core.upstream import upstream_call

#: Lock dimensions that can't be negative; a negative value is a sentinel.
LOCK_DIMENSION_FIELDS: tuple[str, ...] = (
    "lock-width",
    "lock-length",
    "normal-lock-lift",
    "maximum-lock-lift",
    "volume-per-lockage",
    "minimum-draft",
)

#: Unit system requested from CDA, matching `describe_place`'s EN location.
_UNIT = "EN"


def lookup(
    office: str,
    name: str,
    location_kind: str | None,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Locks at a PROJECT (each of its locks) or LOCK (itself) location.

    Returns `{lock_lookup_status, locks[, lock_upstream_status]}`. Any other
    kind is `not_applicable` and makes no upstream call.
    """
    kind = (location_kind or "").upper()
    if kind == "LOCK":
        names = [name]
    elif kind == "PROJECT":
        try:
            names = _lock_names(office, name, use_cache=use_cache)
        except CwmsToolsError as err:
            return _with_status({"lock_lookup_status": "failed", "locks": []}, err)
    else:
        return {"lock_lookup_status": "not_applicable", "locks": []}

    records: list[dict[str, Any]] = []
    first_error: CwmsToolsError | None = None
    for lock_name in names:
        try:
            records.append(_lock_detail(office, lock_name, use_cache=use_cache))
        except CwmsToolsError as err:
            first_error = first_error or err
            records.append({"name": lock_name, "error": err.envelope.code.value})
    failures = sum(1 for r in records if "error" in r)
    if failures == 0:
        status = "ok"
    elif failures == len(records):
        status = "failed"
    else:
        status = "partial"
    out: dict[str, Any] = {"lock_lookup_status": status, "locks": records}
    return _with_status(out, first_error) if first_error is not None else out


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Lift identity to the top, null negative dimension sentinels (new dict)."""
    record = dict(payload)
    invalid = [
        field
        for field in LOCK_DIMENSION_FIELDS
        if isinstance(value := record.get(field), (int, float)) and value < 0
    ]
    for field in invalid:
        record[field] = None
    location = record.get("location") or {}
    return {
        "name": location.get("name"),
        "public-name": location.get("public-name"),
        **record,
        "invalid_fields": invalid,
    }


def _lock_names(office: str, project: str, *, use_cache: bool) -> list[str]:
    cache = get_cache()
    key = build_cache_key("locks", office, "list", project, api_root=current_config().api_root)
    if use_cache and (hit := cache.get(key)) is not None:
        return hit
    with upstream_call("/projects/locks", operation=f"lock list for {project}"):
        data = locks_api.get_locks(office_id=office, project_id=project)
    payload = data.json if hasattr(data, "json") else data
    if not isinstance(payload, list) or not all(isinstance(e, dict) for e in payload):
        raise _malformed("/projects/locks", operation=f"lock list for {project}")
    names = [n for entry in payload if isinstance(n := _lock_name(entry), str)]
    cache.set(key, names, ttl=cache.ttl_for("location_catalog"))
    return names


def _lock_detail(office: str, name: str, *, use_cache: bool) -> dict[str, Any]:
    cache = get_cache()
    key = build_cache_key(
        "locks", office, "detail", name, _UNIT, api_root=current_config().api_root
    )
    if use_cache and (hit := cache.get(key)) is not None:
        return dict(hit)
    with upstream_call(f"/projects/locks/{name}", operation="lock lookup"):
        data = locks_api.get_lock(name=name, office_id=office, unit=_UNIT)
    payload = data.json if hasattr(data, "json") else data
    if not isinstance(payload, dict) or _lock_name(payload) is None:
        raise _malformed(f"/projects/locks/{name}", operation="lock lookup")
    record = normalize(payload)
    cache.set(key, record, ttl=cache.ttl_for("location_catalog"))
    return dict(record)


def _lock_name(entry: dict[str, Any]) -> str | None:
    location = entry.get("location")
    name = location.get("name") if isinstance(location, dict) else None
    return name if isinstance(name, str) else None


def _malformed(endpoint: str, *, operation: str) -> CwmsToolsError:
    """A 200 whose body isn't a lock payload (e.g. an HTML error page, which
    cwms-python returns as a string). Raised before caching, so it degrades
    like any other failed lock call and is retried next time."""
    return CwmsToolsError.of(
        ErrorCode.UPSTREAM_ERROR,
        f"{operation}: unexpected upstream response shape for {endpoint}.",
        endpoints_called=[endpoint],
        temporary=True,
    )


def _with_status(out: dict[str, Any], err: CwmsToolsError) -> dict[str, Any]:
    """Attach the failing call's HTTP status, when the failure carried one."""
    cause = err.__cause__
    status = getattr(getattr(cause, "response", None), "status_code", None)
    if isinstance(cause, ApiError) and isinstance(status, int):
        out["lock_upstream_status"] = status
    return out


__all__ = ["LOCK_DIMENSION_FIELDS", "lookup", "normalize"]
