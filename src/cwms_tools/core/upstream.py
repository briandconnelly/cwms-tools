"""Translate cwms-python / HTTP failures into structured `CwmsToolsError`s.

`core.errors` stays decoupled from any specific upstream client; this module is
the one place that knows cwms-python's exception types. Wrap each upstream call
in `upstream_call(...)` so a failure reaches the MCP `_safe` / CLI `emit_error`
boundary as an envelope instead of a raw exception (#131):

- `cwms.api.ApiError` → classified by HTTP status (`upstream_error_from_status`):
  404 not_found, 429 rate_limited, other 4xx permanent, 5xx temporary. A 2xx
  `ApiError` means cwms-python could not parse the body (1.1.x raises instead
  of returning the text) and is reported as an unreadable, temporary failure.
- `requests.RequestException` (incl. `ConnectionError`, `Timeout`,
  `RetryError`) and `urllib3.exceptions.HTTPError` → temporary upstream_error.

Messages are bounded: operation, endpoint, and the status or exception class —
never `str(exc)`, whose URL and response body can run to kilobytes. The
original exception stays reachable as `__cause__`. Anything else (a
programming error) propagates untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeGuard

from cwms.api import ApiError
from requests import RequestException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    retry_after_ms_from_response,
    upstream_error_from_status,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def failure_summary(exc: BaseException) -> str:
    """Bounded description of an upstream failure: the HTTP status or the class name.

    For call sites that keep their own error code/temporary classification but
    must not interpolate `str(exc)` (#136).
    """
    if isinstance(exc, ApiError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int) and 200 <= status < 300:
            return f"unreadable upstream response (HTTP {status})"
        return f"HTTP {status}" if status else "upstream error"
    return type(exc).__name__


def from_api_error(exc: ApiError, *, endpoint: str, operation: str) -> CwmsToolsError:
    """Status-classified envelope for an `ApiError`, with a bounded message."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and 200 <= status < 300:
        message = f"{operation}: unreadable upstream response (HTTP {status}) for {endpoint}."
    else:
        message = f"{operation}: upstream returned {status or 'an error'} for {endpoint}."
    return upstream_error_from_status(
        status,
        endpoint=endpoint,
        message=message,
        retry_after_ms=retry_after_ms_from_response(response),
    )


def from_transport_error(exc: BaseException, *, endpoint: str, operation: str) -> CwmsToolsError:
    """Temporary envelope for a connection/timeout/retry failure (no HTTP status)."""
    return CwmsToolsError.of(
        ErrorCode.UPSTREAM_ERROR,
        f"{operation}: upstream request to {endpoint} failed ({type(exc).__name__}).",
        endpoints_called=[endpoint],
        temporary=True,
    )


def is_record(payload: Any) -> TypeGuard[dict[str, Any]]:
    """True when a singleton endpoint returned a usable record: a non-empty object.

    cwms-python 1.1.x returns `{}` for an empty 200/204 body, so `{}` (or a
    non-object body) from a single-record endpoint is an anomaly, not a record
    (#137). Don't use this for list endpoints, where a real empty result is
    a valid collection: validate their shape with `require_collection` or the
    endpoint's own parser instead, since `{}` is an anomaly there too (#152).
    """
    return isinstance(payload, dict) and bool(payload)


def require_record(
    payload: Any, *, endpoint: str, operation: str, field: str, value: str
) -> dict[str, Any]:
    """Return `payload` if it is a record, else raise a temporary upstream_error.

    Callers must check before caching, so the anomaly is never served from cache.
    """
    if is_record(payload):
        return payload
    raise CwmsToolsError.of(
        ErrorCode.UPSTREAM_ERROR,
        f"{operation}: upstream returned an empty or non-object body for {endpoint}.",
        field=field,
        value=value,
        endpoints_called=[endpoint],
        temporary=True,
    )


def empty_collection_error(
    *, endpoint: str, operation: str, field: str, value: str
) -> CwmsToolsError:
    """Temporary upstream_error for a list endpoint whose body has no collection.

    cwms-python 1.1.x returns `{}` for an empty 200/204 body. On a list
    endpoint a real empty result still carries its collection key (e.g.
    `{"entries": []}`), so a body without one is an anomaly, not "none" (#152).
    Callers must check before caching.
    """
    return CwmsToolsError.of(
        ErrorCode.UPSTREAM_ERROR,
        f"{operation}: upstream returned an empty or unrecognized collection body for {endpoint}.",
        field=field,
        value=value,
        endpoints_called=[endpoint],
        temporary=True,
    )


def require_collection(
    payload: Any, *, keys: tuple[str, ...], endpoint: str, operation: str, field: str, value: str
) -> dict[str, Any]:
    """Return `payload` if one of `keys` holds a list, else raise `empty_collection_error`.

    For list payloads with a flat shape; parsers that accept nested shapes
    decide for themselves and raise `empty_collection_error` directly.
    """
    if isinstance(payload, dict) and any(isinstance(payload.get(k), list) for k in keys):
        return payload
    raise empty_collection_error(endpoint=endpoint, operation=operation, field=field, value=value)


@contextmanager
def upstream_call(
    endpoint: str, *, operation: str, page_selector: str | None = None
) -> Iterator[None]:
    """Re-raise upstream failures inside the block as `CwmsToolsError`.

    `page_selector` is for a block that pages through `cwms.api.get_with_paging`:
    an empty body on page 2+ makes it index `{}` by that selector, and the
    resulting `KeyError(page_selector)` is reported as an empty-page upstream
    failure (#152). Any other `KeyError` propagates untouched.
    """
    try:
        yield
    except KeyError as exc:
        if page_selector is None or exc.args != (page_selector,):
            raise
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            f"{operation}: upstream returned an empty page while paginating {endpoint}.",
            endpoints_called=[endpoint],
            temporary=True,
        ) from exc
    except ApiError as exc:
        raise from_api_error(exc, endpoint=endpoint, operation=operation) from exc
    except (RequestException, Urllib3HTTPError) as exc:
        raise from_transport_error(exc, endpoint=endpoint, operation=operation) from exc


__all__ = [
    "empty_collection_error",
    "failure_summary",
    "from_api_error",
    "from_transport_error",
    "is_record",
    "require_collection",
    "require_record",
    "upstream_call",
]
