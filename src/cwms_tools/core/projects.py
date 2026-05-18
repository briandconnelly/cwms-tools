"""Project metadata + the get_project format-error fallback.

The upstream wrapper raises a format error on some PROJECTs (e.g. NWDM/FTPK
returns `"Formatting error: No Format for this content-type and data-type..."`
— cwms-overview.md §8). We catch the documented marker string and synthesize
a partial response from the underlying Location, with `project_metadata: null`
and `partial_reasons: ["get_project_format_error"]`.
"""

from __future__ import annotations

from typing import Any

from cwms.projects.projects import get_project as _upstream_get_project

from cwms_tools.core import locations
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.session import current_config

# Substring fingerprint of the upstream format-error. Conservative: needs to
# tolerate minor wording drift but not match unrelated errors.
_FORMAT_ERROR_MARKER = "No Format for this content-type and data-type"


def get_one(office_id: str, name: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Return the Project payload, or a partial fallback when upstream format-errors.

    Response shape:

    - On success: the upstream Project payload plus `partial: false`.
    - On the documented format-error: the bare Location payload under
      `project_metadata: null`, `partial: true`,
      `partial_reasons: ["get_project_format_error"]`.
    - On any other upstream failure: `CwmsToolsError(ErrorCode.UPSTREAM_ERROR)`.
    """
    cache = get_cache()
    cfg = current_config()
    key = build_cache_key("location_catalog", office_id, "project", name, api_root=cfg.api_root)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit

    try:
        data = _upstream_get_project(office_id=office_id, name=name)
    except Exception as exc:
        if _FORMAT_ERROR_MARKER in str(exc):
            fallback = _format_error_fallback(office_id, name, use_cache=use_cache, error=str(exc))
            cache.set(key, fallback, ttl=cache.ttl_for("location_catalog"))
            return fallback
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            f"upstream get_project failed for {office_id}/{name}: {exc}",
            endpoints_called=[f"/projects/{office_id}/{name}"],
        ) from exc

    payload = data.json if hasattr(data, "json") else data
    out = {
        "project_metadata": payload,
        "partial": False,
        "partial_reasons": [],
        "source_workaround": None,
    }
    cache.set(key, out, ttl=cache.ttl_for("location_catalog"))
    return out


def _format_error_fallback(
    office_id: str,
    name: str,
    *,
    use_cache: bool,
    error: str,
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
    }


__all__ = ["get_one"]
