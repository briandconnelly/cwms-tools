"""Tests for the four place tools, exercised at the core layer."""

from __future__ import annotations

import cwms
import pytest
import responses

from cwms_tools.core import locations, places, projects, session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.geo import BBox

API_ROOT = "https://example.test/cwms-data/"


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", API_ROOT)
    monkeypatch.delenv("CWMS_TOOLS_OPERATOR_EMAIL", raising=False)
    session._state["config"] = None
    cwms.init_session(api_root=API_ROOT, pool_connections=4)
    session.configure_session()
    cache = Cache(directory=tmp_path / "cache")
    set_cache(cache)
    yield
    cache.close()
    set_cache(None)
    session._state["config"] = None


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rmock:
        yield rmock


LOCATIONS_PAYLOAD = {
    "locations": [
        {
            "office-id": "SWT",
            "name": "FOSS",
            "public-name": "Foss Reservoir",
            "location-kind": "PROJECT",
            "latitude": 35.55,
            "longitude": -98.97,
            "state-initial": "OK",
        },
        {
            "office-id": "SWT",
            "name": "CHOU-Lock",
            "public-name": "Choctaw Lock",
            "location-kind": "LOCK",
            "latitude": 33.97,
            "longitude": -94.95,
            "state-initial": "OK",
        },
    ],
}

TIMESERIES_PAYLOAD = {
    "entries": [
        {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", "last-update": "2026-05-17T18:00:00Z"},
        {
            "name": "FOSS.Flow-Out.Inst.15Minutes.0.Best-MRBWM",
            "last-update": "2026-05-17T17:45:00Z",
        },
    ],
}

LOCATION_SINGLE_PAYLOAD = {
    "office-id": "SWT",
    "name": "FOSS",
    "location-kind": "PROJECT",
    "latitude": 35.55,
    "longitude": -98.97,
    "public-name": "Foss Reservoir",
    "long-name": "Foss Reservoir, OK",
    "horizontal-datum": "NAD83",
    "state-initial": "OK",
    "timezone-name": "America/Chicago",
}

PROJECT_PAYLOAD = {
    "location": LOCATION_SINGLE_PAYLOAD,
    "authorizing-law": "Flood Control Act of 1944",
    "project-owner": "USACE",
}


def _arm_all(mocked):
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=LOCATIONS_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json=TIMESERIES_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        json=LOCATION_SINGLE_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/FOSS",
        json=PROJECT_PAYLOAD,
        status=200,
    )


# --------------------------------------------------------------------------
# search_places
# --------------------------------------------------------------------------


def test_search_places_returns_enriched_matches(configured, mocked) -> None:
    _arm_all(mocked)
    payload = places.search_places("FOSS", office="SWT")
    assert payload["query"] == "FOSS"
    assert payload["office"] == "SWT"
    # FOSS is data-bearing and must come first; CHOU-Lock should not match the query.
    assert payload["results"][0]["name"] == "FOSS"
    assert payload["results"][0]["parameter_count"] == 2


def test_search_places_sorts_ghosts_after_data_bearing(configured, mocked) -> None:
    # An empty `like` (i.e. the query never narrows the rows because our test
    # client-side filter is a no-op for "" query) returns both.
    _arm_all(mocked)
    payload = places.search_places("", office="SWT")
    names = [r["name"] for r in payload["results"]]
    # FOSS (parameter_count=2) sorts before CHOU-Lock (parameter_count=0).
    assert names.index("FOSS") < names.index("CHOU-Lock")


# --------------------------------------------------------------------------
# describe_place — including the get_project format-error fallback
# --------------------------------------------------------------------------


def test_describe_place_combines_all_subcalls(configured, mocked) -> None:
    _arm_all(mocked)
    payload = places.describe_place("SWT", "FOSS")
    assert payload["office_id"] == "SWT"
    assert payload["name"] == "FOSS"
    assert payload["project"] is not None
    assert payload["partial"] is False
    assert payload["parameter_count"] == 2
    publishers_seen = {p["publisher"] for p in payload["publishers"]}
    assert "Best-MRBWM" in publishers_seen
    assert "Ccp-Rev" in publishers_seen
    assert payload["last_data_timestamp"] == "2026-05-17T18:00:00Z"


def test_describe_place_falls_back_on_get_project_format_error(configured, mocked) -> None:
    """The documented format-error response must trigger the Location fallback."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=LOCATIONS_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json=TIMESERIES_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        json=LOCATION_SINGLE_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/FOSS",
        json={
            "message": (
                "Formatting error: No Format for this content-type and data-type "
                "(application/json;version=2, cwms.cda.data.dto.project.Project)"
            ),
        },
        status=406,
    )
    project_resp = projects.get_one("SWT", "FOSS")
    assert project_resp["partial"] is True
    assert "get_project_format_error" in project_resp["partial_reasons"]
    assert project_resp["project_metadata"] is None
    assert project_resp["source_workaround"] == "project_format_error_fallback"
    assert project_resp["upstream_status"] == 406


def test_describe_place_falls_back_when_location_is_not_a_project(
    configured, mocked
) -> None:
    """A 404 from /projects/{name} means the location is real but not a project.
    Degrade to partial: true with `not_a_project` instead of raising
    UPSTREAM_ERROR. Real-world case: NWDP/UBLW depth-string sensors."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        json=LOCATION_SINGLE_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/FOSS",
        status=404,
        body="Not Found",
    )
    project_resp = projects.get_one("SWT", "FOSS", use_cache=False)
    assert project_resp["partial"] is True
    assert "not_a_project" in project_resp["partial_reasons"]
    assert project_resp["project_metadata"] is None
    assert project_resp["upstream_status"] == 404
    # The format-error workaround marker must NOT be reused for the 404 case.
    assert project_resp["source_workaround"] is None


def test_describe_place_falls_back_when_project_lookup_is_other_4xx(
    configured, mocked
) -> None:
    """Any 4xx that isn't 404 or the documented 406 format-error becomes a
    `project_lookup_4xx` partial. Surfaces the upstream status so the agent
    can decide whether to dig further."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        json=LOCATION_SINGLE_PAYLOAD,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/FOSS",
        status=400,
        body="Bad Request",
    )
    project_resp = projects.get_one("SWT", "FOSS", use_cache=False)
    assert project_resp["partial"] is True
    assert "project_lookup_4xx" in project_resp["partial_reasons"]
    assert project_resp["upstream_status"] == 400


def test_describe_place_raises_upstream_error_on_project_5xx(
    configured, mocked
) -> None:
    """5xx is transient; we do NOT swallow it into a partial response —
    raise UPSTREAM_ERROR(retryable=True) so the caller can back off and
    retry."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/FOSS",
        status=503,
        body="Service Unavailable",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        projects.get_one("SWT", "FOSS", use_cache=False)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.retryable is True


# --------------------------------------------------------------------------
# list_parameters
# --------------------------------------------------------------------------


def test_list_parameters_groups_by_publisher(configured, mocked) -> None:
    _arm_all(mocked)
    payload = places.list_parameters("SWT", "FOSS")
    assert payload["ts_count"] == 2
    publishers_seen = {p["publisher"] for p in payload["by_publisher"]}
    assert publishers_seen == {"Best-MRBWM", "Ccp-Rev"}
    assert set(payload["all_parameters"]) == {"Elev", "Flow-Out"}


# --------------------------------------------------------------------------
# browse_region
# --------------------------------------------------------------------------


def test_browse_region_filters_by_state(configured, mocked) -> None:
    _arm_all(mocked)
    payload = places.browse_region(office="SWT", state="OK")
    assert payload["result_count"] == 2
    assert payload["ghost_count"] == 1


def test_browse_region_filters_by_bbox(configured, mocked) -> None:
    _arm_all(mocked)
    # Bounding box that contains only FOSS (~35.55, -98.97) — not CHOU-Lock.
    bbox = BBox(south=35.0, west=-100.0, north=36.0, east=-98.0)
    payload = places.browse_region(office="SWT", bbox=bbox)
    names = [r["name"] for r in payload["results"]]
    assert names == ["FOSS"]


# --------------------------------------------------------------------------
# locations.get_one wraps upstream errors via status code
# --------------------------------------------------------------------------


def test_locations_get_one_wraps_5xx_as_retryable_upstream_error(configured, mocked) -> None:
    """A transient 5xx must surface as UPSTREAM_ERROR(retryable=True), not
    masquerade as NOT_FOUND. The previous bare `except Exception` collapsed
    every upstream failure into a 'not found' envelope."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        status=503,
        body="Service Unavailable",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        locations.get_one("SWT", "FOSS", use_cache=False)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.retryable is True


def test_locations_get_one_wraps_404_as_not_found_with_field(configured, mocked) -> None:
    """A genuine 404 still maps to NOT_FOUND and carries `field`/`offending_value`
    so the agent gets a useful repair surface."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/MISSING",
        status=404,
        body="Not Found",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        locations.get_one("SWT", "MISSING", use_cache=False)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.NOT_FOUND
    assert env.field == "name"
    assert env.offending_value == "MISSING"
