"""Project metadata + fallbacks for upstream get_project failures.

Recoverable failure modes:

- Non-project locations (e.g. NWDP/UBLW, NWDP/UBLW_S1-D21,0ft): upstream
  returns 404. The location is real and useful; we degrade to a partial
  response rather than raising UPSTREAM_ERROR.
- An empty or non-object body (cwms-python 1.1.2+ returns `{}` for an empty
  200/204, #137) → partial with `project_lookup_empty`. Unlike the 404/4xx
  fallbacks this is an anomaly, not a fact about the place, so it is not
  cached.
- Other 4xx → partial with `project_lookup_4xx` + the captured status. This
  includes the 406 "No Format for this content-type and data-type" that
  cwms-python <= 1.0.8 triggered for every project by requesting the v2 JSON
  format; 1.0.9 fixed it by requesting v1 (the declared floor is now 1.1.2).

5xx → propagated as a temporary UPSTREAM_ERROR.
"""

from __future__ import annotations

from typing import Any

from cwms.api import ApiError
from cwms.projects.projects import get_project as _upstream_get_project

from cwms_tools.core import locations
from cwms_tools.core.cache import build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.session import current_config
from cwms_tools.core.upstream import failure_summary, is_record


def get_one(office_id: str, name: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Return the Project payload, or a partial fallback for recoverable failures.

    Returned shape:

    - Success: `{project_metadata: <payload>, partial: false, partial_reasons: [],
      source_workaround: null, upstream_status: null}`.
    - 404 (location is not a project): `partial: true, partial_reasons:
      ["not_a_project"]`, `upstream_status: 404`.
    - Other 4xx: `partial: true, partial_reasons: ["project_lookup_4xx"]`,
      `upstream_status: <code>`.
    - Empty or non-object body: `partial: true, partial_reasons:
      ["project_lookup_empty"]`, `upstream_status: null`; not cached.
    - 5xx: raises `CwmsToolsError(UPSTREAM_ERROR, temporary=True)`.
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
                f"upstream get_project failed for {office_id}/{name} ({failure_summary(exc)}).",
                endpoints_called=[endpoint],
                temporary=True,
            ) from exc
        cache.set(key, fallback, ttl=cache.ttl_for("location_catalog"))
        return fallback
    except Exception as exc:  # defensive for non-ApiError surprises (transport errors)
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            f"upstream get_project failed for {office_id}/{name} ({failure_summary(exc)}).",
            endpoints_called=[endpoint],
            temporary=True,
        ) from exc

    payload = data.json if hasattr(data, "json") else data
    if not is_record(payload):
        # Returned before the cache.set below: a transient anomaly must not be
        # served from cache the way the deterministic 404/4xx fallbacks are.
        return _location_only_fallback(
            office_id,
            name,
            use_cache=use_cache,
            partial_reason="project_lookup_empty",
            upstream_status=None,
        )
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

    Returns None when the caller should raise UPSTREAM_ERROR (temporary
    5xx, or anything else outside the documented partial-response paths).
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)

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


def _location_only_fallback(
    office_id: str,
    name: str,
    *,
    use_cache: bool,
    partial_reason: str,
    upstream_status: int | None,
) -> dict[str, Any]:
    """Return the Location with project_metadata=null; used for 404 / other 4xx / empty body."""
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
