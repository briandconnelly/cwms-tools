"""Project metadata + fallbacks for upstream get_project failures.

Two recoverable failure modes:

- Documented format-error (e.g. NWDM/FTPK returns 406 with
  `"No Format for this content-type and data-type"`; cwms-overview.md §8).
- Non-project locations (e.g. NWDP/UBLW, NWDP/UBLW_S1-D21,0ft): upstream
  returns 404. The location is real and useful; we degrade to a partial
  response rather than raising UPSTREAM_ERROR.

Other 4xx → partial with `project_lookup_4xx` + the captured status.
5xx → propagated as a retryable UPSTREAM_ERROR.
"""

from __future__ import annotations

from typing import Any

from cwms.api import ApiError
from cwms.projects.projects import get_project as _upstream_get_project

from cwms_tools.core import locations
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.session import current_config

# Substring fingerprint of the upstream format-error. Conservative: needs to
# tolerate minor wording drift but not match unrelated errors.
_FORMAT_ERROR_MARKER = "No Format for this content-type and data-type"


def get_one(office_id: str, name: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Return the Project payload, or a partial fallback for recoverable failures.

    Returned shape:

    - Success: `{project_metadata: <payload>, partial: false, partial_reasons: [],
      source_workaround: null, upstream_status: null}`.
    - Format-error fallback: `partial: true, partial_reasons:
      ["get_project_format_error"]`, `source_workaround:
      "project_format_error_fallback"`, `upstream_status: 406`.
    - 404 (location is not a project): `partial: true, partial_reasons:
      ["not_a_project"]`, `upstream_status: 404`.
    - Other 4xx: `partial: true, partial_reasons: ["project_lookup_4xx"]`,
      `upstream_status: <code>`.
    - 5xx: raises `CwmsToolsError(UPSTREAM_ERROR, retryable=True)`.
    """
    cache = get_cache()
    cfg = current_config()
    key = build_cache_key("location_catalog", office_id, "project", name, api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit

    endpoint = f"/projects/{office_id}/{name}"
    try:
        data = _upstream_get_project(office_id=office_id, name=name)
    except ApiError as exc:
        fallback = _classify_and_fallback(exc, office_id, name, use_cache=use_cache)
        if fallback is None:
            raise CwmsToolsError.of(
                ErrorCode.UPSTREAM_ERROR,
                f"upstream get_project failed for {office_id}/{name}: {exc}",
                endpoints_called=[endpoint],
                retryable=True,
            ) from exc
        cache.set(key, fallback, ttl=cache.ttl_for("location_catalog"))
        return fallback
    except Exception as exc:  # pragma: no cover - defensive for non-ApiError surprises
        if _FORMAT_ERROR_MARKER in str(exc):
            # Pre-existing path: some cwms-python versions raise a bare
            # Exception for the format-error rather than an ApiError.
            fallback = _format_error_fallback(
                office_id, name, use_cache=use_cache, error=str(exc), upstream_status=None
            )
            cache.set(key, fallback, ttl=cache.ttl_for("location_catalog"))
            return fallback
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            f"upstream get_project failed for {office_id}/{name}: {exc}",
            endpoints_called=[endpoint],
            retryable=True,
        ) from exc

    payload = data.json if hasattr(data, "json") else data
    out = {
        "project_metadata": payload,
        "partial": False,
        "partial_reasons": [],
        "source_workaround": None,
        "upstream_status": None,
    }
    cache.set(key, out, ttl=cache.ttl_for("location_catalog"))
    return out


def _classify_and_fallback(
    exc: ApiError,
    office_id: str,
    name: str,
    *,
    use_cache: bool,
) -> dict[str, Any] | None:
    """Route an upstream ApiError to a partial-response fallback or None.

    Returns None when the caller should raise UPSTREAM_ERROR (retryable
    5xx, or anything else outside the documented partial-response paths).
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    message = str(exc)

    # Documented 406 format error — same shape across both NWDM/FTPK-style
    # observations and the rarer cases where ApiError carries the message.
    if _FORMAT_ERROR_MARKER in message:
        return _format_error_fallback(
            office_id, name, use_cache=use_cache, error=message, upstream_status=status
        )

    if status == 404:
        return _location_only_fallback(
            office_id,
            name,
            use_cache=use_cache,
            partial_reason="not_a_project",
            upstream_status=status,
        )

    if isinstance(status, int) and 400 <= status < 500:
        return _location_only_fallback(
            office_id,
            name,
            use_cache=use_cache,
            partial_reason="project_lookup_4xx",
            upstream_status=status,
        )

    return None


def _format_error_fallback(
    office_id: str,
    name: str,
    *,
    use_cache: bool,
    error: str,
    upstream_status: int | None,
) -> dict[str, Any]:
    """Synthesize a partial response from the underlying Location."""
    location = locations.get_one(office_id, name, use_cache=use_cache)
    return {
        "project_metadata": None,
        "location_fallback": location,
        "partial": True,
        "partial_reasons": ["get_project_format_error"],
        "source_workaround": "project_format_error_fallback",
        "upstream_error_excerpt": error[:200],
        "upstream_status": upstream_status,
    }


def _location_only_fallback(
    office_id: str,
    name: str,
    *,
    use_cache: bool,
    partial_reason: str,
    upstream_status: int | None,
) -> dict[str, Any]:
    """Return the Location with project_metadata=null; used for 404 / other 4xx."""
    location = locations.get_one(office_id, name, use_cache=use_cache)
    return {
        "project_metadata": None,
        "location_fallback": location,
        "partial": True,
        "partial_reasons": [partial_reason],
        "source_workaround": None,
        "upstream_status": upstream_status,
    }


__all__ = ["get_one"]
