"""Tests for the four place tools, exercised at the core layer."""

from __future__ import annotations

import re

import cwms
import pytest
import responses

from cwms_tools.core import catalog, locations, offices, pagination, places, projects, session
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


def test_search_places_echoes_canonical_office_scope(configured, mocked) -> None:
    """`office` echoes the canonical scope, beside `offices_searched` (not the raw arg)."""
    _arm_all(mocked)
    assert places.search_places("FOSS", office="swt")["office"] == "SWT"
    listed = places.search_places("FOSS", office=["swt", "SWT", "mvs"])
    assert listed["office"] == ["SWT", "MVS"]
    assert places.search_places("FOSS")["office"] is None


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


def test_describe_place_degrades_on_406_format_error_as_generic_4xx(configured, mocked) -> None:
    """cwms-python <= 1.0.8 got a 406 "No Format" for every project (it asked
    for the v2 JSON format). With the 1.0.9 floor that special case is gone, so
    a 406 must still degrade gracefully — via the generic 4xx path, with no
    workaround marker."""
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
    project_resp = projects.get_one("SWT", "FOSS", use_cache=False)
    assert project_resp["partial"] is True
    assert project_resp["partial_reasons"] == ["project_lookup_4xx"]
    assert project_resp["project_metadata"] is None
    assert project_resp["source_workaround"] is None
    assert project_resp["upstream_status"] == 406


def test_describe_place_falls_back_when_location_is_not_a_project(configured, mocked) -> None:
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
    assert project_resp["source_workaround"] is None


def test_describe_place_falls_back_when_project_lookup_is_other_4xx(configured, mocked) -> None:
    """Any 4xx that isn't 404 becomes a `project_lookup_4xx` partial. Surfaces
    the upstream status so the agent can decide whether to dig further."""
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


def test_describe_place_raises_upstream_error_on_project_5xx(configured, mocked) -> None:
    """5xx is transient; we do NOT swallow it into a partial response —
    raise UPSTREAM_ERROR(temporary=True) so the caller can back off and
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
    assert env.temporary is True


# --------------------------------------------------------------------------
# Empty singleton bodies (#137): cwms-python 1.1.2+ returns `{}` for an empty
# 200/204 body. Driven through the real client with mocked HTTP.
# --------------------------------------------------------------------------

EMPTY_BODIES = pytest.mark.parametrize("status", [200, 204], ids=["empty-200", "204"])


def _calls_to(mocked, path: str) -> int:
    return sum(1 for c in mocked.calls if c.request.url.split("?")[0] == f"{API_ROOT}{path}")


@EMPTY_BODIES
def test_location_empty_body_raises_and_is_not_cached(configured, mocked, status) -> None:
    url = f"{API_ROOT}locations/FOSS"
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, json=LOCATION_SINGLE_PAYLOAD, status=200)

    for _ in range(2):
        with pytest.raises(CwmsToolsError) as ex_info:
            locations.get_one("SWT", "FOSS")
        env = ex_info.value.envelope
        assert env.code is ErrorCode.UPSTREAM_ERROR
        assert env.temporary is True
        assert "empty or non-object body" in env.message
        assert env.details is not None
        assert (env.details.field, env.details.value) == ("name", "FOSS")
        assert env.source.endpoints_called == ["/locations/FOSS"]

    # Control: a real record is cached, so the empties above were not.
    assert locations.get_one("SWT", "FOSS") == LOCATION_SINGLE_PAYLOAD
    assert locations.get_one("SWT", "FOSS") == LOCATION_SINGLE_PAYLOAD
    assert _calls_to(mocked, "locations/FOSS") == 3


@EMPTY_BODIES
def test_project_empty_body_degrades_to_partial_and_is_not_cached(
    configured, mocked, status
) -> None:
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", json=LOCATION_SINGLE_PAYLOAD)
    url = f"{API_ROOT}projects/FOSS"
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, json=PROJECT_PAYLOAD, status=200)

    first = projects.get_one("SWT", "FOSS")
    assert first["partial"] is True
    assert first["partial_reasons"] == ["project_lookup_empty"]
    assert first["project_metadata"] is None
    assert first["upstream_status"] is None
    assert first["location_fallback"] == LOCATION_SINGLE_PAYLOAD

    # Not cached: the next call re-asks upstream and gets the real project,
    # which is cached (control).
    second = projects.get_one("SWT", "FOSS")
    assert second["partial"] is False
    assert second["project_metadata"] == PROJECT_PAYLOAD
    assert projects.get_one("SWT", "FOSS") == second
    assert _calls_to(mocked, "projects/FOSS") == 2


def test_describe_place_reports_empty_project_body_as_partial(configured, mocked) -> None:
    _arm_all(mocked)
    mocked.replace(responses.GET, f"{API_ROOT}projects/FOSS", body="", status=200)
    payload = places.describe_place("SWT", "FOSS")
    assert payload["location"] == LOCATION_SINGLE_PAYLOAD
    assert payload["project"] is None
    assert payload["partial"] is True
    assert payload["partial_reasons"] == ["project_lookup_empty"]
    assert payload["upstream_status"] is None


@pytest.mark.parametrize("body", ["null", "[]", '"FOSS"'])
def test_location_non_object_body_raises(configured, mocked, body) -> None:
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        body=body,
        status=200,
        content_type="application/json",
    )
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", json=LOCATION_SINGLE_PAYLOAD)
    with pytest.raises(CwmsToolsError) as ex_info:
        locations.get_one("SWT", "FOSS")
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert env.details is not None
    assert (env.details.field, env.details.value) == ("name", "FOSS")
    # Not cached: the next call re-asks upstream and gets the real record.
    assert locations.get_one("SWT", "FOSS") == LOCATION_SINGLE_PAYLOAD
    assert _calls_to(mocked, "locations/FOSS") == 2


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


def test_browse_region_limit_truncates_and_reports_total_count(configured, mocked) -> None:
    """M2/C4: browse caps results and reports the full size + a repair hint.

    #73: `truncated` means unrecoverable-by-paging; a `limit` cap here is
    fully pageable via `next_cursor`, so `truncated` stays False — `has_more`
    is the signal to page, not `truncated`."""
    _arm_all(mocked)
    payload = places.browse_region(office="SWT", limit=1)
    assert payload["result_count"] == 1
    assert payload["total_count"] == 2
    assert payload["truncated"] is False
    assert payload["has_more"] is True
    assert payload["limit"] == 1
    assert "truncation_hint" in payload
    # Data-bearing FOSS sorts ahead of the CHOU-Lock ghost, so the cap keeps it.
    assert payload["results"][0]["name"] == "FOSS"


def test_browse_region_no_cap_when_limit_none(configured, mocked) -> None:
    _arm_all(mocked)
    payload = places.browse_region(office="SWT", limit=None)
    assert payload["truncated"] is False
    assert payload["result_count"] == payload["total_count"] == 2
    assert "truncation_hint" not in payload


def test_browse_region_results_include_parameters_and_data_at(configured, mocked) -> None:
    """Missed-B: browse rows carry `parameters` and `data_at`, not just the
    fields that were always populated — so the typed PlaceSummary fields aren't
    silently empty."""
    _arm_all(mocked)
    payload = places.browse_region(office="SWT")
    foss = next(r for r in payload["results"] if r["name"] == "FOSS")
    assert set(foss["parameters"]) == {"Elev", "Flow-Out"}
    assert "data_at" in foss  # present on every row (empty for data-bearing FOSS)


# --------------------------------------------------------------------------
# data_at repair hint for barren parents (e.g. UBLW_S1 -> UBLW_S1-D21,0ft)
# --------------------------------------------------------------------------


def test_search_places_carries_data_at_repair_for_barren_parents(configured, mocked) -> None:
    """When a barren parent location has a co-located data-bearing child,
    `data_at` lists the child names so the agent doesn't have to walk
    co_located manually."""
    locations_payload = {
        "locations": [
            {
                "office-id": "NWDP",
                "name": "UBLW_S1",
                "public-name": "University Bridge Lake Washington (Parent)",
                "latitude": 47.65,
                "longitude": -122.32,
            },
            {
                "office-id": "NWDP",
                "name": "UBLW_S1-D21,0ft",
                "public-name": "University Bridge Lake Washington -21ft",
                "latitude": 47.65,
                "longitude": -122.32,
            },
        ]
    }
    timeseries_payload = {
        "entries": [
            # Only the depth-tagged child carries ts ids.
            {"name": "UBLW_S1-D21,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
        ]
    }
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=locations_payload,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json=timeseries_payload,
        status=200,
    )
    payload = places.search_places("University Bridge", office="NWDP")
    by_name = {r["name"]: r for r in payload["results"]}
    parent = by_name["UBLW_S1"]
    child = by_name["UBLW_S1-D21,0ft"]
    assert parent["parameter_count"] == 0
    assert parent["data_at"] == ["UBLW_S1-D21,0ft"]
    # Data-bearing rows get an empty data_at — no repair needed.
    assert child["parameter_count"] >= 1
    assert child["data_at"] == []


def test_list_parameters_carries_data_at_when_location_is_barren(configured, mocked) -> None:
    """A direct `place parameters` call against a barren parent should hint
    at the depth-tagged children that actually carry data."""
    locations_payload = {
        "locations": [
            {
                "office-id": "NWDP",
                "name": "UBLW_S1",
                "latitude": 47.65,
                "longitude": -122.32,
            },
            {
                "office-id": "NWDP",
                "name": "UBLW_S1-D21,0ft",
                "latitude": 47.65,
                "longitude": -122.32,
            },
        ]
    }
    # First call (ts_ids_for_location, scoped to UBLW_S1) returns nothing
    # because the parent has no ts ids.
    parent_ts = {"entries": []}
    # Second call (enrich_locations -> get_timeseries_catalog without `like`)
    # returns the child's ts id so the data_at lookup finds it.
    full_ts = {
        "entries": [
            {"name": "UBLW_S1-D21,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
        ]
    }
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json=parent_ts,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json=parent_ts,  # freshness_for_location: also scoped to UBLW_S1
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=locations_payload,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json=full_ts,
        status=200,
    )
    payload = places.list_parameters("NWDP", "UBLW_S1")
    assert payload["ts_count"] == 0
    assert payload["data_at"] == ["UBLW_S1-D21,0ft"]


def test_list_parameters_data_at_is_null_when_location_has_data(configured, mocked) -> None:
    """When the location itself is data-bearing, no repair hint is needed —
    `data_at` is null."""
    _arm_all(mocked)
    payload = places.list_parameters("SWT", "FOSS")
    assert payload["ts_count"] >= 1
    assert payload["data_at"] is None


def test_data_at_is_declared_on_response_schemas() -> None:
    """`data_at` must be a declared field on the response models, not just
    tolerated by `extra="allow"`. FastMCP only documents declared fields in
    the schema agents read."""
    from cwms_tools.core.models import ListParametersResponse, PlaceSummary

    assert "data_at" in PlaceSummary.model_fields
    assert "data_at" in ListParametersResponse.model_fields


# --------------------------------------------------------------------------
# search_places — parameter filter + broader data_at + budgeted fanout
# --------------------------------------------------------------------------


def _fremont_locations_payload() -> dict[str, object]:
    """Locations near Fremont Bridge (NWDP). Only the parent `FBLW` carries
    "Fremont Bridge" in public-name — the depth-tagged sensors are id-only,
    so a natural-language query for "Fremont Bridge" does NOT find them
    via name-search alone. This is exactly the shape the Codex probe found
    in the wild."""
    return {
        "locations": [
            {
                "office-id": "NWDP",
                "name": "FBLW",
                "public-name": "Fremont Bridge",
                "latitude": 47.65,
                "longitude": -122.35,
            },
            {
                "office-id": "NWDP",
                "name": "FBLW_D1-D5,0ft",
                # Deliberately NO "Fremont Bridge" in any searchable field.
                "latitude": 47.65,
                "longitude": -122.35,
            },
            {
                "office-id": "NWDP",
                "name": "FBLW_D1-D18,0ft",
                "latitude": 47.65,
                "longitude": -122.35,
            },
        ]
    }


def _fremont_ts_payload() -> dict[str, object]:
    """The parent `FBLW` carries only `Volt-Battery`; depth-tagged children
    are the real `Temp-Water` sensors. Mirrors the Codex probe."""
    return {
        "entries": [
            {"name": "FBLW.Volt-Battery.Inst.1Hour.0.IRIDIUM-REV"},
            {"name": "FBLW_D1-D5,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
            {"name": "FBLW_D1-D18,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
        ]
    }


def _arm_fremont(mocked, *, copies: int = 6) -> None:
    """Mock the locations + ts catalog calls for the Fremont search.

    The search runs:
    - LOCATIONS once (cached after) + TIMESERIES with `like=^(FBLW)\\.`
    - Then a broader fallback for data_at: unfiltered LOCATIONS (cache hit)
      + unfiltered TIMESERIES (different cache key, miss)
    Arm enough copies so multi-office or chained tests don't run out."""
    locs = _fremont_locations_payload()
    ts = _fremont_ts_payload()
    for _ in range(copies):
        mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locs, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts, status=200)


def test_search_places_parameter_filter_drops_non_publishers(configured, mocked) -> None:
    """The Fremont Bridge probe: ask for Temp-Water. The parent `FBLW`
    (Volt-Battery only) must be filtered out — but kept ONLY if its
    `data_at` siblings publish Temp-Water (which they do here, via the
    broader-catalog fallback)."""
    _arm_fremont(mocked)
    payload = places.search_places("Fremont Bridge", office="NWDP", parameter="Temp-Water")
    names = [r["name"] for r in payload["results"]]
    # FBLW is data-bearing (Volt-Battery) but doesn't publish Temp-Water,
    # AND it's not barren — must be dropped entirely.
    assert "FBLW" not in names
    # The dropped data-bearing row is reflected in the count.
    assert payload["nearby_non_matching_count"] >= 1
    assert payload["parameter"] == "Temp-Water"


def test_search_places_broader_data_at_expands_beyond_query_match(configured, mocked) -> None:
    """When a barren parent matches the natural query but its real
    data-bearing depth-tagged children do NOT match the query, the
    `data_at` lookup must fall back to the full office catalog and
    surface those children."""
    locs = {
        "locations": [
            {
                "office-id": "NWDP",
                "name": "PARENT",
                "public-name": "Lonely Site",
                "latitude": 47.65,
                "longitude": -122.35,
            },
            # Depth child has NO "Lonely Site" anywhere — won't match the query.
            {
                "office-id": "NWDP",
                "name": "PARENT-D5,0ft",
                "latitude": 47.65,
                "longitude": -122.35,
            },
        ]
    }
    # PARENT publishes nothing; PARENT-D5,0ft publishes Temp-Water.
    ts = {"entries": [{"name": "PARENT-D5,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"}]}
    for _ in range(4):
        mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locs, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts, status=200)
    payload = places.search_places("Lonely Site", office="NWDP")
    by_name = {r["name"]: r for r in payload["results"]}
    parent = by_name["PARENT"]
    assert parent["parameter_count"] == 0
    # Without the broader fallback, data_at would be empty because
    # PARENT-D5,0ft never landed in the filtered search results.
    assert parent["data_at"] == ["PARENT-D5,0ft"]


def test_search_places_with_office_list_searches_each(configured, mocked) -> None:
    """Passing an explicit list of offices fans out across each one. The
    response carries `offices_searched` reflecting what was actually
    queried."""
    nwdp_locs = _fremont_locations_payload()
    nwdp_ts = _fremont_ts_payload()
    # SWT has nothing matching `Fremont Bridge`.
    swt_locs = {"locations": []}
    swt_ts = {"entries": []}
    # Each office runs filtered + unfiltered fetches via the data_at
    # broader fallback path; arm enough to satisfy the cold-cache traffic.
    for _ in range(4):
        mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=nwdp_locs, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=nwdp_ts, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=swt_locs, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=swt_ts, status=200)
    payload = places.search_places("Fremont Bridge", office=["NWDP", "SWT"])
    assert payload["offices_searched"] == ["NWDP", "SWT"]
    assert payload["offices_skipped_for_budget"] == []
    names = [r["name"] for r in payload["results"]]
    assert "FBLW" in names


def test_search_places_single_ghost_office_raises_ghost_office(configured, mocked) -> None:
    """A single NW-stub office must surface the ghost_office envelope, not empty results.

    #69: `repair` is None at this core level — a same-tool retry needs the
    calling surface's own name/args, which `mcp.tools._safe`/`cli.render`
    attach at the boundary (see test_mcp_tool_handlers.py/test_cli_place.py
    for the surface-level repair assertions)."""
    with pytest.raises(CwmsToolsError) as exc_info:
        places.search_places("Bear Creek", office="NWO")
    env = exc_info.value.envelope
    assert env.code is ErrorCode.GHOST_OFFICE
    assert env.repair is None


def test_search_places_multi_office_records_failed_office_as_partial(configured, mocked) -> None:
    """In a multi-office fan-out, one failing office degrades to partial, not silence."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json={
            "locations": [
                {"office-id": "SWT", "name": "FOSS", "latitude": 35.55, "longitude": -98.97}
            ]
        },
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    resp = places.search_places("FOSS", office=["SWT", "NWO"])
    assert resp["partial"] is True
    assert "NWO: ghost_office" in resp["partial_reasons"]
    assert resp["offices_searched"] == ["SWT", "NWO"]


def test_search_places_no_office_arg_uses_cached_scope_only(configured, mocked) -> None:
    """When `office` is omitted and nothing is cached, the fanout default
    is empty — the response should not silently expand to every office.
    The caller is told via `partial: true` + `partial_reasons`."""
    payload = places.search_places("Fremont Bridge")
    assert payload["offices_searched"] == []
    assert payload["results"] == []
    assert payload["partial"] is True
    assert any("no_offices_in_scope" in r for r in payload["partial_reasons"])


def test_search_places_empty_scope_returns_repair_hint(configured, mocked) -> None:
    """#24: a bare-name search with nothing in scope is otherwise a dead end.
    Surface a structured `repair_hint` that names a concrete, ready-to-use
    data-bearing office list to retry with, instead of the generic
    'omit or pass a list' string."""
    payload = places.search_places("Gas Works", parameter="Temp-Water")
    hint = payload["repair_hint"]
    assert hint["reason"] == "no_offices_in_scope"
    assert hint["tool"] == "cwms_search_places"
    # The hint echoes the caller's query/parameter so it is copy-paste retryable.
    assert hint["args"]["query"] == "Gas Works"
    assert hint["args"]["parameter"] == "Temp-Water"
    # And names a non-empty, concrete office list (the curated data-bearing set).
    assert hint["args"]["office"] == offices.discovery_office_candidates()
    assert hint["args"]["office"]  # non-empty


def test_search_places_with_office_has_no_repair_hint(configured, mocked) -> None:
    """The repair hint is a dead-end recovery affordance only — once an office
    is in scope (here, results come back) the response carries no hint."""
    _arm_fremont(mocked)
    payload = places.search_places("Fremont Bridge", office="NWDP")
    assert "repair_hint" not in payload


def test_search_places_explicit_empty_office_has_no_repair_hint(configured, mocked) -> None:
    """An explicit empty list (`office=[]`) is a deliberate 'search nothing' —
    the hint is only the bare-name/implicit-scope recovery (office omitted), so
    an explicit empty scope must NOT be widened with a hint."""
    payload = places.search_places("Gas Works", office=[], parameter="Temp-Water")
    assert payload["offices_searched"] == []
    assert "repair_hint" not in payload


def test_search_places_normalizes_string_office_to_unchanged_response(configured, mocked) -> None:
    """Passing `office="NWDP"` (string) still works after the type widening.
    Verifies backwards compatibility for callers that have an office in hand."""
    _arm_fremont(mocked)
    payload = places.search_places("Fremont Bridge", office="NWDP")
    # `office` echoes the caller's input shape, not the normalized list.
    assert payload["office"] == "NWDP"
    assert payload["offices_searched"] == ["NWDP"]
    assert any(r["name"] == "FBLW" for r in payload["results"])


def test_search_places_caps_uncached_office_fanout_by_budget(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the caller passes more uncached offices than the per-call
    budget allows, the overflow lands in `offices_skipped_for_budget`
    with a repair hint embedded in the response (via the agent's next
    call). Budget mirrors publishers_index — small ceil(MAX_WORKERS/2)."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    # Two uncached offices, budget of 1 → only one runs.
    locs = {"locations": []}
    ts = {"entries": []}
    for _ in range(2):
        mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locs, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts, status=200)
    payload = places.search_places("anything", office=["NWDM", "NWDP", "SWT"])
    assert len(payload["offices_searched"]) == 1
    assert sorted(payload["offices_skipped_for_budget"]) == sorted(
        o for o in ["NWDM", "NWDP", "SWT"] if o not in payload["offices_searched"]
    )


def test_search_places_skipped_offices_coexist_with_truncated_false(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#73 review: `truncated: false` describes row completeness within the
    *searched* offices only — it must NOT be conflated with (or imply)
    scope completeness. A response can simultaneously have `has_more: true`
    (more rows to page through in the searched office) AND non-empty
    `offices_skipped_for_budget` (an office never searched at all);
    `truncated` stays `false` regardless, since `offices_skipped_for_budget`
    — not `truncated` — is the dedicated signal for scope incompleteness."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    locations_payload = {
        "locations": [
            {"office-id": "NWDP", "name": f"Site_{i:04d}", "latitude": 47.0, "longitude": -122.0}
            for i in range(75)
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locations_payload, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    payload = places.search_places("Site", office=["NWDP", "SWT"])
    assert payload["offices_searched"] == ["NWDP"]
    assert payload["offices_skipped_for_budget"] == ["SWT"]
    assert payload["has_more"] is True
    assert payload["truncated"] is False


# --------------------------------------------------------------------------
# search_places --limit truncation
# --------------------------------------------------------------------------


def test_search_places_caps_result_count_by_default(configured, mocked) -> None:
    """Broad searches should be capped so agents don't get flooded. Default
    cap is 50; rows past the cap are dropped and the response carries
    `has_more: true` plus the full `total_count` — `truncated` stays False
    since `next_cursor` can page through the rest (#73)."""
    locations_payload = {
        "locations": [
            {
                "office-id": "NWDP",
                "name": f"Site_{i:04d}",
                "public-name": f"Temp String Site #{i}",
                "latitude": 47.0,
                "longitude": -122.0,
            }
            for i in range(75)
        ]
    }
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=locations_payload,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": []},
        status=200,
    )
    payload = places.search_places("Temp String", office="NWDP")
    assert payload["total_count"] == 75
    assert payload["truncated"] is False
    assert payload["has_more"] is True
    assert payload["limit"] == 50
    assert len(payload["results"]) == 50


def test_search_places_no_limit_returns_all(configured, mocked) -> None:
    """`limit=None` disables the cap entirely. Used by agents that have a
    legitimate reason to enumerate every match."""
    locations_payload = {
        "locations": [
            {
                "office-id": "NWDP",
                "name": f"Site_{i:04d}",
                "latitude": 47.0,
                "longitude": -122.0,
            }
            for i in range(75)
        ]
    }
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=locations_payload,
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": []},
        status=200,
    )
    payload = places.search_places("Site", office="NWDP", limit=None)
    assert payload["truncated"] is False
    assert len(payload["results"]) == 75


# --------------------------------------------------------------------------
# locations.get_one wraps upstream errors via status code
# --------------------------------------------------------------------------


def test_locations_get_one_wraps_5xx_as_temporary_upstream_error(configured, mocked) -> None:
    """A transient 5xx must surface as UPSTREAM_ERROR(temporary=True), not
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
    assert env.temporary is True


def test_locations_get_one_wraps_404_as_not_found_with_field(configured, mocked) -> None:
    """A genuine 404 still maps to NOT_FOUND and carries `details.field`/`details.value`
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
    assert env.details is not None
    assert env.details.field == "name"
    assert env.details.value == "MISSING"


# --------------------------------------------------------------------------
# search_places — cursor pagination
# --------------------------------------------------------------------------


def test_search_places_paginates_with_cursor(monkeypatch):
    rows = [
        {
            "office_id": "NWDM",
            "name": f"L{i}",
            "parameter_count": 1,
            "parameters": ["Elev"],
            "publishers": [],
            "co_located": [],
        }
        for i in range(5)
    ]
    fanout_calls = 0

    def _counting_fanout(req, **kwargs):
        nonlocal fanout_calls
        fanout_calls += 1
        return (["NWDM"], [], [])

    monkeypatch.setattr(places, "_run_fanout", _counting_fanout)
    monkeypatch.setattr(
        places,
        "_gather_enriched",
        lambda offices, q, use_cache, reraise_single: (list(rows), [], []),
    )

    page1 = places.search_places("L", office="NWDM", limit=2)
    assert len(page1["results"]) == 2
    assert page1["has_more"] is True
    assert page1["total_count"] == 5
    assert page1["next_cursor"]

    page2 = places.search_places("L", office="NWDM", limit=2, cursor=page1["next_cursor"])
    assert [r["name"] for r in page2["results"]] == ["L2", "L3"]
    assert page2["has_more"] is True
    assert page2["next_cursor"] is not None

    page3 = places.search_places("L", office="NWDM", limit=2, cursor=page2["next_cursor"])
    assert [r["name"] for r in page3["results"]] == ["L4"]
    assert page3["has_more"] is False
    assert page3["next_cursor"] is None

    # #72: every page — including cursor continuations — re-runs `_run_fanout`
    # over the (locked) office set, so a forged cursor can't bypass the
    # uncached-office budget. Legitimate continuations pay no real extra cost
    # since the locked offices are cache-hot by the time a page 2/3 call
    # arrives; see the forged-cursor regression test below for the case this
    # closes.
    assert fanout_calls == 3


def test_search_places_cursor_continuation_echoes_canonical_office(monkeypatch):
    rows = [
        {
            "office_id": "SWT",
            "name": f"L{i}",
            "parameter_count": 1,
            "parameters": ["Elev"],
            "publishers": [],
            "co_located": [],
        }
        for i in range(3)
    ]
    monkeypatch.setattr(places, "_run_fanout", lambda req, **kw: (list(req), [], []))
    monkeypatch.setattr(
        places,
        "_gather_enriched",
        lambda offices, q, use_cache, reraise_single: (list(rows), [], []),
    )
    page1 = places.search_places("L", office="swt", limit=2)
    assert page1["office"] == "SWT"
    page2 = places.search_places("L", office="swt", limit=2, cursor=page1["next_cursor"])
    assert page2["office"] == "SWT"
    assert page2["offices_searched"] == ["SWT"]


def test_search_places_cursor_rejects_catalog_shift(monkeypatch):
    five = [
        {
            "office_id": "NWDM",
            "name": f"L{i}",
            "parameter_count": 1,
            "parameters": [],
            "publishers": [],
            "co_located": [],
        }
        for i in range(5)
    ]
    six = [
        *five,
        {
            "office_id": "NWDM",
            "name": "L5",
            "parameter_count": 1,
            "parameters": [],
            "publishers": [],
            "co_located": [],
        },
    ]
    monkeypatch.setattr(places, "_run_fanout", lambda req, **kwargs: (["NWDM"], [], []))
    calls = {"n": 0}

    def _shifting_gather(offices, q, use_cache, reraise_single):
        calls["n"] += 1
        return (list(five if calls["n"] == 1 else six), [], [])  # catalog grows between calls

    monkeypatch.setattr(places, "_gather_enriched", _shifting_gather)
    page1 = places.search_places("L", office="NWDM", limit=2)
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("L", office="NWDM", limit=2, cursor=page1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_rejects_mismatched_cursor(monkeypatch):
    rows = [
        {
            "office_id": "NWDM",
            "name": f"L{i}",
            "parameter_count": 1,
            "parameters": [],
            "publishers": [],
            "co_located": [],
        }
        for i in range(5)
    ]
    monkeypatch.setattr(places, "_run_fanout", lambda req, **kwargs: (["NWDM"], [], []))
    monkeypatch.setattr(
        places,
        "_gather_enriched",
        lambda offices, q, use_cache, reraise_single: (list(rows), [], []),
    )
    page1 = places.search_places("L", office="NWDM", limit=2)
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("DIFFERENT", office="NWDM", limit=2, cursor=page1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_rejects_cursor_with_unlimited_limit(monkeypatch):
    # A cursor combined with an unlimited limit (0 -> None) is contradictory and
    # must be rejected before any fan-out, not silently return a tail subset.
    monkeypatch.setattr(places, "_run_fanout", lambda req, **kwargs: (["NWDM"], [], []))
    monkeypatch.setattr(
        places, "_gather_enriched", lambda offices, q, use_cache, reraise_single: ([], [], [])
    )
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("L", office="NWDM", limit=0, cursor="anytoken")
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_forged_cursor_cannot_bypass_fanout_budget(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#72: cursors are unkeyed base64url(JSON) and `req` is an unkeyed hash
    of the query/parameter (`pagination.request_hash`) — anyone can build one
    without ever having called `search_places`. A forged cursor naming many
    never-cached offices must be capped by the SAME per-call uncached-office
    budget the fresh path enforces (`_run_fanout` re-applied on continuation),
    not trusted wholesale. When re-budgeting drops any of the cursor's locked
    offices, the call is rejected outright (`invalid_cursor`) rather than
    silently searching a smaller set than promised — proven here by asserting
    ZERO upstream calls were made, not just the raised error code."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)

    req = pagination.request_hash({"q": "anything", "parameter": None})
    forged = pagination.encode_cursor(
        {
            "v": pagination.CURSOR_VERSION,
            "kind": "search_places",
            "off": 0,
            "req": req,
            # None of these are cached — a fresh call with this same list
            # would budget them identically; the cursor path must too.
            "offices": ["NWDM", "NWDP", "SWT"],
            "total": 0,
        }
    )
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("anything", limit=2, cursor=forged)
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR
    # The real security property: no upstream calls were made at all — the
    # budget-skip is detected and rejected BEFORE any fan-out, not after
    # spending calls on a partial/degraded set.
    assert len(mocked.calls) == 0


def test_search_places_cursor_rejects_when_cache_expires_between_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#72 (Codex review): pre-#72, a cursor's locked offices were refetched
    unconditionally regardless of cache state, so cache TTL eviction between
    pages never affected a legitimate multi-office cursor. Re-applying the
    budget on continuation means an office that falls out of cache between
    pages can now get budget-skipped too — this is an intentional, narrow
    behavior change (documented in `_resolve_scope`): the response is a
    clearly labeled `invalid_cursor` restart signal, not a silently smaller
    result set that a client might mistake for complete."""
    rows = [
        {
            "office_id": "NWDM",
            "name": f"L{i}",
            "parameter_count": 1,
            "parameters": [],
            "publishers": [],
            "co_located": [],
        }
        for i in range(3)
    ]
    cached_offices = {"NWDM", "NWDP"}
    monkeypatch.setattr(places, "_fanout_budget", lambda: 0)  # no new fetches allowed at all
    monkeypatch.setattr(
        catalog, "location_catalog_cached", lambda office, like=None: office in cached_offices
    )
    monkeypatch.setattr(
        places,
        "_gather_enriched",
        lambda offices, q, use_cache, reraise_single: (list(rows), [], []),
    )

    page1 = places.search_places("L", office=["NWDM", "NWDP"], limit=2)
    assert page1["offices_searched"] == ["NWDM", "NWDP"]
    assert page1["next_cursor"]

    cached_offices.discard("NWDP")  # simulate TTL eviction between page 1 and page 2
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("L", office=["NWDM", "NWDP"], limit=2, cursor=page1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_browse_region_paginates_with_cursor(monkeypatch):
    rows = [
        {
            "office_id": "SWT",
            "name": f"B{i}",
            "parameter_count": 1,
            "parameters": [],
            "publishers": [],
            "co_located": [],
        }
        for i in range(5)
    ]
    monkeypatch.setattr(
        places.catalog, "enrich_locations", lambda office, use_cache=True: list(rows)
    )
    p1 = places.browse_region(office="SWT", limit=2)
    assert p1["has_more"] is True and p1["total_count"] == 5 and p1["next_cursor"]
    p2 = places.browse_region(office="SWT", limit=2, cursor=p1["next_cursor"])
    assert [r["name"] for r in p2["results"]] == ["B2", "B3"]
    assert p2["has_more"] is True


def test_browse_region_cursor_rejects_mismatch(monkeypatch):
    rows = [
        {
            "office_id": "SWT",
            "name": f"B{i}",
            "parameter_count": 1,
            "parameters": [],
            "publishers": [],
            "co_located": [],
        }
        for i in range(5)
    ]
    monkeypatch.setattr(
        places.catalog, "enrich_locations", lambda office, use_cache=True: list(rows)
    )
    p1 = places.browse_region(office="SWT", limit=2)
    # changing the state filter invalidates the cursor (req hash differs)
    with pytest.raises(CwmsToolsError) as exc:
        places.browse_region(office="SWT", state="OK", limit=2, cursor=p1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_browse_region_rejects_cursor_with_unlimited_limit(monkeypatch):
    monkeypatch.setattr(places.catalog, "enrich_locations", lambda office, use_cache=True: [])
    with pytest.raises(CwmsToolsError) as exc:
        places.browse_region(office="SWT", limit=0, cursor="anytoken")
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_multi_office_records_unexpected_error_as_partial(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-CwmsToolsError failure in a multi-office fan-out degrades to partial."""
    real_search = locations.search

    def flaky_search(office_id, query, *, use_cache=True):
        if office_id == "SWT":
            raise RuntimeError("boom")
        return real_search(office_id, query, use_cache=use_cache)

    monkeypatch.setattr(locations, "search", flaky_search)
    # NWO short-circuits locally via the catalog ghost-office guard, so no HTTP
    # mocks are needed for it; SWT fails before any request.
    resp = places.search_places("FOSS", office=["SWT", "NWO"])
    assert resp["partial"] is True
    assert "SWT: internal_error (RuntimeError)" in resp["partial_reasons"]
    assert "NWO: ghost_office" in resp["partial_reasons"]


def test_search_places_single_office_reraises_unexpected_error(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(office_id, query, *, use_cache=True):
        raise RuntimeError("boom")

    monkeypatch.setattr(locations, "search", boom)
    with pytest.raises(RuntimeError):
        places.search_places("FOSS", office="SWT")


# --- browse_region `kind` filter (#133) --------------------------------------


def _kind_rows() -> list[dict[str, object]]:
    def row(name, kind, state, count, co_located=()):
        return {
            "office_id": "SWT",
            "name": name,
            "location_kind": kind,
            "parameter_count": count,
            "parameters": ["Elev"] if count else [],
            "publishers": [],
            "co_located": list(co_located),
            "raw": {"state": state} if state else {},
        }

    no_kind = row("NOKIND", None, "OK", 1)
    del no_kind["location_kind"]
    return [
        row("DAM1", "PROJECT", "OK", 3, co_located=["DAM1-GAGE"]),
        row("dam2", "project", "TX", 1),
        row("LOCK1", "LOCK", "OK", 0),
        row("GHOSTDAM", "Project", "OK", 0, co_located=["DAM1-SITE"]),
        # Data-bearing sibling of GHOSTDAM that the PROJECT filter excludes.
        row("DAM1-SITE", "SITE", "OK", 2),
        row("DAM1-GAGE", None, "OK", 1),
        no_kind,
    ]


@pytest.fixture
def kind_catalog(monkeypatch):
    monkeypatch.setattr(
        places.catalog, "enrich_locations", lambda office, use_cache=True: _kind_rows()
    )


def test_browse_region_cursor_minted_on_fallback_case_validates_after_recovery(
    kind_catalog,
) -> None:
    """T11: on the directory fallback `swt` passes through un-canonicalized; once
    the directory recovers the same office arrives as `SWT`. The cursor must
    survive that, so the request hash is built on the upper-cased code."""
    first = places.browse_region(office="swt", limit=2)
    assert first["has_more"] is True
    second = places.browse_region(office="SWT", limit=2, cursor=first["next_cursor"])
    assert second["results"]
    assert {r["name"] for r in second["results"]}.isdisjoint({r["name"] for r in first["results"]})


def test_browse_region_kind_filter_is_case_insensitive(kind_catalog) -> None:
    for kind in ("PROJECT", "project", "  Project "):
        payload = places.browse_region(office="SWT", kind=kind)
        assert {r["name"] for r in payload["results"]} == {"DAM1", "dam2", "GHOSTDAM"}
        assert payload["kind"] == "PROJECT"


def test_browse_region_kind_excludes_rows_without_kind(kind_catalog) -> None:
    payload = places.browse_region(office="SWT", kind="SITE")
    # DAM1-GAGE (location_kind None) and NOKIND (key absent) never match.
    assert [r["name"] for r in payload["results"]] == ["DAM1-SITE"]


def test_browse_region_kind_combines_with_state_and_counts_filtered_set(kind_catalog) -> None:
    payload = places.browse_region(office="SWT", kind="project", state="OK", limit=1)
    assert payload["total_count"] == 2  # DAM1 + GHOSTDAM; dam2 is TX
    assert payload["ghost_count"] == 1  # GHOSTDAM
    assert payload["result_count"] == 1
    assert [r["name"] for r in payload["results"]] == ["DAM1"]  # data-bearing first
    assert payload["has_more"] is True


def test_browse_region_kind_absent_from_payload_when_unset(kind_catalog) -> None:
    payload = places.browse_region(office="SWT")
    assert "kind" not in payload
    assert payload["total_count"] == 7


@pytest.mark.parametrize("blank", ["", "   "])
def test_browse_region_blank_kind_is_usage_error(kind_catalog, blank) -> None:
    with pytest.raises(CwmsToolsError) as exc:
        places.browse_region(office="SWT", kind=blank)
    env = exc.value.envelope
    assert env.code is ErrorCode.USAGE_ERROR
    assert env.details is not None and env.details.field == "kind"


def test_browse_region_cursor_from_other_kind_is_rejected(kind_catalog) -> None:
    p1 = places.browse_region(office="SWT", kind="PROJECT", limit=1)
    assert p1["next_cursor"]
    with pytest.raises(CwmsToolsError) as exc:
        places.browse_region(office="SWT", kind="LOCK", limit=1, cursor=p1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR
    # The same kind in another case normalizes to the same request: still valid.
    p2 = places.browse_region(office="SWT", kind="project", limit=1, cursor=p1["next_cursor"])
    assert p2["result_count"] == 1


def test_browse_region_cursor_without_kind_rejected_when_kind_added(kind_catalog) -> None:
    p1 = places.browse_region(office="SWT", limit=2)
    with pytest.raises(CwmsToolsError) as exc:
        places.browse_region(office="SWT", kind="PROJECT", limit=2, cursor=p1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_browse_region_no_kind_request_hash_is_unchanged(kind_catalog) -> None:
    """A pre-#133 cursor (hash over office/bbox/state only) must still validate."""
    legacy_req = pagination.request_hash({"office": "SWT", "bbox": None, "state": "OK"})
    assert places._browse_request_hash(office="SWT", bbox=None, state="OK", kind=None) == legacy_req
    legacy_cursor = pagination.encode_cursor(
        {
            "v": pagination.CURSOR_VERSION,
            "kind": "browse_region",
            "off": 1,
            "req": legacy_req,
            "total": 6,  # rows with state OK
        }
    )
    page = places.browse_region(office="SWT", state="OK", limit=1, cursor=legacy_cursor)
    assert page["result_count"] == 1


def test_browse_region_kind_data_at_uses_full_catalog(kind_catalog) -> None:
    payload = places.browse_region(office="SWT", kind="PROJECT")
    ghost = next(r for r in payload["results"] if r["name"] == "GHOSTDAM")
    # DAM1-SITE is a SITE (filtered out by kind) but still named as a data sibling.
    assert ghost["data_at"] == ["DAM1-SITE"]


def test_search_places_skipped_offices_always_yield_partial_and_a_followable_hint(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#161: the reviewer's second call searched 4 of 19 offices, got
    `partial:false` and no hint, and concluded Fort Peck was not in CWMS. Any
    budget-skipped office must set partial and hand back exactly the skipped
    list as the next `office` argument — regardless of whether rows came back."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES_PAYLOAD, status=200)
    payload = places.search_places("Fort Peck", office=["SWT", "NWDM", "NWDP"], parameter="Elev")
    assert payload["results"] == []
    assert payload["offices_skipped_for_budget"] == ["NWDM", "NWDP"]
    assert payload["partial"] is True
    assert any(r.startswith("offices_skipped_for_budget:") for r in payload["partial_reasons"])
    hint = payload["repair_hint"]
    assert hint["reason"] == "offices_skipped_for_budget"
    assert hint["tool"] == "cwms_search_places"
    assert hint["args"] == {"query": "Fort Peck", "office": ["NWDM", "NWDP"], "parameter": "Elev"}


def test_search_places_budget_skip_after_a_match_with_data_is_optional(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#191: once a match with data is in hand, the budget hint keeps its
    followable `args` but switches to a reason an agent can branch on to stop."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES_PAYLOAD, status=200)
    payload = places.search_places("FOSS", office=["SWT", "NWDM", "NWDP"], parameter="Elev")
    assert payload["results"][0]["parameter_count"] > 0
    assert payload["offices_skipped_for_budget"] == ["NWDM", "NWDP"]
    assert payload["partial"] is True
    hint = payload["repair_hint"]
    assert hint["reason"] == "match_found_offices_skipped"
    assert hint["tool"] == "cwms_search_places"
    assert hint["args"] == {"query": "FOSS", "office": ["NWDM", "NWDP"], "parameter": "Elev"}
    assert "optional" in hint["message"] and "2 office(s)" in hint["message"]
    assert "scope_note" not in payload
    # Following it is still a valid continuation over exactly the skipped offices.
    nxt = places.search_places(**hint["args"])
    assert nxt["offices_searched"] == ["NWDM"]
    assert nxt["offices_skipped_for_budget"] == ["NWDP"]


def test_search_places_cold_chain_reaches_every_curated_office(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow the hint chain from a cold cache until no office is skipped: every
    curated office must be searched exactly once, richest first."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 4)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    first = places.search_places("Fort Peck")
    assert first["results"] == [] and first["partial"] is True
    assert first["repair_hint"]["reason"] == "no_offices_in_scope"
    assert "while one is present" in first["repair_hint"]["message"]
    searched: list[str] = []
    hint = first["repair_hint"]
    for _ in range(10):
        resp = places.search_places(**hint["args"])
        searched.extend(resp["offices_searched"])
        if "repair_hint" not in resp:
            assert resp.get("partial", False) is False
            break
        hint = resp["repair_hint"]
        assert hint["reason"] == "offices_skipped_for_budget"
        assert "while one is present" in hint["message"]
        assert "cursor" not in hint["args"] and "limit" not in hint["args"]
    assert searched[:4] == ["NWDM", "NWDP", "SWT", "MVS"]
    assert searched == offices.discovery_office_candidates()


def test_search_places_no_offices_in_scope_reason_does_not_tell_caller_to_omit_office(
    configured, mocked
) -> None:
    payload = places.search_places("Fort Peck")
    reason = next(r for r in payload["partial_reasons"] if r.startswith("no_offices_in_scope"))
    assert "omit" not in reason
    assert "repair_hint" in reason


# --- partly-warm cache + temporary-error carry-forward (#161, #164) ----------


def _warm_swt_empty(mocked) -> None:
    """Index SWT's locations catalog through the real producer (empty catalog)."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    places.search_places("zzz-warmup", office="SWT")


def test_search_places_partly_warm_cache_without_match_carries_a_continuation(
    configured, mocked
) -> None:
    """#161: with only SWT cached the implicit scope is ["SWT"]; an empty result
    there must not read as 'absent' — it names the curated offices never searched."""
    _warm_swt_empty(mocked)
    payload = places.search_places("Barkley")
    assert payload["offices_searched"] == ["SWT"]
    assert payload["results"] == []
    assert payload["partial"] is True
    assert any(r.startswith("cached_scope_incomplete:") for r in payload["partial_reasons"])
    hint = payload["repair_hint"]
    assert hint["reason"] == "cached_scope_incomplete"
    assert hint["tool"] == "cwms_search_places"
    assert hint["args"] == {
        "query": "Barkley",
        "office": [o for o in offices.discovery_office_candidates() if o != "SWT"],
    }


def test_search_places_partly_warm_cache_with_a_match_has_no_hint(configured, mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES_PAYLOAD, status=200)
    places.search_places("zzz-warmup", office="SWT")  # warms SWT with matching rows
    payload = places.search_places("FOSS")
    assert payload["offices_searched"] == ["SWT"]
    assert payload["results"]
    assert "repair_hint" not in payload
    assert "partial" not in payload


def _flaky_locations(mocked, *, failing_office: str | set[str], fail_first_only: bool):
    """Locations-catalog callback: `failing_office`(es) answer 500 (once, or always)."""
    import json as _json
    from urllib.parse import parse_qs, urlparse

    calls = {"n": 0}
    failing = {failing_office} if isinstance(failing_office, str) else failing_office

    def cb(request):
        office = parse_qs(urlparse(request.url).query).get("office", [""])[0]
        if office in failing:
            calls["n"] += 1
            if not fail_first_only or calls["n"] == 1:
                return (500, {}, _json.dumps({"message": "boom"}))
        return (200, {}, _json.dumps({"locations": []}))

    mocked.add_callback(
        responses.GET, f"{API_ROOT}catalog/LOCATIONS", callback=cb, content_type="application/json"
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)


def test_search_places_temporary_error_is_reported_not_carried_into_the_hint(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(places, "_fanout_budget", lambda: 2)
    _flaky_locations(mocked, failing_office="SWT", fail_first_only=True)
    first = places.search_places("Barkley", office=["NWDM", "SWT", "MVS"])
    assert first["offices_skipped_for_budget"] == ["MVS"]
    assert "SWT: upstream_error" in first["partial_reasons"]
    assert any(r.startswith("temporary_errors_not_retried: SWT ") for r in first["partial_reasons"])
    hint = first["repair_hint"]
    assert hint["reason"] == "offices_skipped_for_budget"
    assert hint["args"]["office"] == ["MVS"]
    assert "temporary" not in hint["message"]
    second = places.search_places(**hint["args"])
    assert second["offices_searched"] == ["MVS"]
    assert "repair_hint" not in second


def test_search_places_persistent_temporary_errors_do_not_chain_forever(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget 1, two offices that both always fail: following the hint must end."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    _flaky_locations(mocked, failing_office={"NWDM", "NWDP"}, fail_first_only=False)
    first = places.search_places("x", office=["NWDM", "NWDP"])
    assert first["offices_skipped_for_budget"] == ["NWDP"]
    hint = first["repair_hint"]
    assert hint["args"]["office"] == ["NWDP"]  # NWDM is reported, not re-carried
    assert any(
        r.startswith("temporary_errors_not_retried: NWDM ") for r in first["partial_reasons"]
    )
    # Hop 2 is a single office with nothing skipped: its failure surfaces as the
    # structured error, so the chain ends here instead of ping-ponging.
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places(**hint["args"])
    assert exc.value.envelope.code is ErrorCode.UPSTREAM_ERROR


def test_search_places_temporary_error_on_the_final_hop_is_reported_not_hinted(
    configured, mocked
) -> None:
    _flaky_locations(mocked, failing_office="SWT", fail_first_only=False)
    payload = places.search_places("Barkley", office=["NWDM", "SWT"])
    assert "repair_hint" not in payload
    assert payload["partial"] is True
    assert any(
        r.startswith("temporary_errors_not_retried: SWT ") for r in payload["partial_reasons"]
    )
    # #187 review: SWT is in `offices_searched` but failed, so the note must not
    # read as a confirmed miss there; it defers to `partial_reasons`.
    assert payload["offices_searched"] == ["NWDM", "SWT"]
    note = payload["scope_note"]["message"]
    assert "was returned" in note and "`partial_reasons` failed" in note


def test_search_places_single_resolved_office_error_does_not_swallow_a_continuation(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget 1 + two requested offices resolves a single office; its upstream
    error must degrade to partial so the NWDP continuation survives."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    _flaky_locations(mocked, failing_office="NWDM", fail_first_only=False)
    payload = places.search_places("x", office=["NWDM", "NWDP"])
    assert "NWDM: upstream_error" in payload["partial_reasons"]
    assert payload["repair_hint"]["args"]["office"] == ["NWDP"]


def test_search_places_explicit_empty_office_reason_says_so(configured, mocked) -> None:
    payload = places.search_places("x", office=[])
    assert payload["partial_reasons"] == [
        "no_offices_in_scope: explicit empty office list; nothing was searched"
    ]
    assert "repair_hint" not in payload


def test_search_places_use_cache_false_spends_budget_on_cached_offices(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced refresh refetches every office, so a cached office is not free."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    places.search_places("warm", office=["SWT", "MVS"])
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    warm = places.search_places("x", office=["SWT", "MVS"])
    assert warm["offices_skipped_for_budget"] == []
    forced = places.search_places("x", office=["SWT", "MVS"], use_cache=False)
    assert forced["offices_searched"] == ["SWT"]
    assert forced["offices_skipped_for_budget"] == ["MVS"]


def test_search_places_implicit_scope_budget_hint_keeps_the_curated_rest(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced refresh over a partly-warm implicit scope can budget-skip a cached
    office; the hint must still name the curated offices never cached."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    places.search_places("warm", office=["SWT", "MVS"])
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    payload = places.search_places("zzz", use_cache=False)
    assert payload["offices_searched"] == ["MVS"]
    assert payload["offices_skipped_for_budget"] == ["SWT"]
    hint = payload["repair_hint"]
    assert hint["reason"] == "offices_skipped_for_budget"
    expected = list(dict.fromkeys(["SWT", *(o for o in offices.discovery_office_candidates())]))
    expected.remove("MVS")
    assert hint["args"]["office"] == expected


def test_search_places_invalid_office_is_partial_in_fanout_and_raises_alone(
    configured, mocked, validating_directory
) -> None:
    mocked.add(
        responses.GET, f"{API_ROOT}offices", json=[{"office-id": "SWT", "type": "DIS"}], status=200
    )
    _arm_all(mocked)
    payload = places.search_places("FOSS", office=["SWT", "swt", "XYZ"])
    assert payload["offices_searched"] == ["SWT"], "canonical codes are deduplicated"
    assert "XYZ: invalid_field" in payload["partial_reasons"]
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("FOSS", office="XYZ")
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD


def test_search_places_reraise_keys_on_requested_count_not_resolved_count(
    configured, mocked, validating_directory
) -> None:
    """`["SWT","XYZ"]` is a two-office ask: an SWT failure degrades to partial and
    keeps the invalid_field reason; `["SWT"]` alone still raises (control)."""
    mocked.add(
        responses.GET, f"{API_ROOT}offices", json=[{"office-id": "SWT", "type": "DIS"}], status=200
    )
    mocked.add(
        responses.GET, re.compile(rf"{re.escape(API_ROOT)}(catalog|locations).*"), status=404
    )
    payload = places.search_places("FOSS", office=["SWT", "XYZ"])
    assert payload["results"] == []
    assert "XYZ: invalid_field" in payload["partial_reasons"]
    assert any(r.startswith("SWT:") for r in payload["partial_reasons"])
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("FOSS", office=["SWT"])
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND


def test_search_places_all_invalid_explicit_offices_raise(
    configured, mocked, validating_directory
) -> None:
    mocked.add(
        responses.GET, f"{API_ROOT}offices", json=[{"office-id": "SWT", "type": "DIS"}], status=200
    )
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("FOSS", office=["XYZ", "ABC"])
    env = ex.value.envelope
    assert env.code is ErrorCode.INVALID_FIELD
    assert env.details is not None and env.details.field == "office_id"
    assert env.repair is not None and env.repair.tool == "cwms_list_offices"


def test_list_parameters_missing_name_is_not_found_with_suggestion(configured, mocked) -> None:
    _arm_all(mocked)
    mocked.replace(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    with pytest.raises(CwmsToolsError) as ex:
        places.list_parameters("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert ex.value.envelope.details is not None
    assert ex.value.envelope.details.field == "name"
    assert ex.value.repair_intent is not None
    assert ex.value.repair_intent.overrides == {"name": "FOSS"}


def test_describe_place_404_name_carries_suggestion(configured, mocked) -> None:
    _arm_all(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json={"detail": "nope"}, status=404)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert "did you mean 'FOSS'" in ex.value.envelope.message
    assert ex.value.repair_intent is not None
    assert ex.value.repair_intent.overrides == {"name": "FOSS"}


def test_describe_place_ghost_carries_data_at(configured, mocked) -> None:
    """#170: describe on a ghost names the co-located records that publish."""
    locations = {
        "locations": [
            {
                "office-id": "NWDM",
                "name": "GARR-Garrison_Dam",
                "latitude": 47.5,
                "longitude": -101.4,
            },
            {"office-id": "NWDM", "name": "GARR", "latitude": 47.5001, "longitude": -101.4},
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locations, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
        json={"entries": []},
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": [{"name": "GARR.Elev.Inst.1Hour.0.Best-MRBWM"}]},
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/GARR-Garrison_Dam",
        json={"office-id": "NWDM", "name": "GARR-Garrison_Dam"},
        status=200,
    )
    mocked.add(
        responses.GET, f"{API_ROOT}projects/GARR-Garrison_Dam", json={"detail": "no"}, status=404
    )
    payload = places.describe_place("NWDM", "GARR-Garrison_Dam")
    assert payload["parameter_count"] == 0
    assert payload["data_at"] == ["GARR"]
    _arm_all(mocked)
    assert places.describe_place("SWT", "FOSS")["data_at"] is None


def _foss_catalogs(mocked, *, locations_status=200):
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=LOCATIONS_PAYLOAD if locations_status == 200 else {"detail": "x"},
        status=locations_status,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)


def test_describe_place_wrong_case_id_is_not_found_not_ghost(configured, mocked) -> None:
    """Live CDA resolves /locations/Foss to FOSS (200); the exact-case ts filter finds none."""
    _foss_catalogs(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json=LOCATION_SINGLE_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}projects/Foss", json={"detail": "no"}, status=404)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert ex.value.envelope.details is not None
    assert ex.value.envelope.details.field == "name"
    assert ex.value.repair_intent is not None
    assert ex.value.repair_intent.overrides == {"name": "FOSS"}


def test_describe_place_wrong_case_id_same_on_cli_and_mcp(configured, mocked) -> None:
    import asyncio
    import json

    from typer.testing import CliRunner

    from cwms_tools.cli.app import app
    from cwms_tools.mcp.server import build_server

    _foss_catalogs(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json=LOCATION_SINGLE_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}projects/Foss", json={"detail": "no"}, status=404)
    res = asyncio.run(
        build_server().call_tool("cwms_describe_place", arguments={"office": "SWT", "name": "Foss"})
    )
    assert res.is_error
    structured = res.structured_content or {}
    mcp_err = structured.get("result", structured)["error"]
    cli = CliRunner().invoke(app, ["place", "describe", "SWT/Foss"])
    assert cli.exit_code != 0
    cli_err = json.loads(cli.stderr)["error"]
    assert mcp_err["code"] == cli_err["code"] == "not_found"
    assert mcp_err["message"] == cli_err["message"]
    assert "FOSS" in mcp_err["message"]
    # Each surface's repair replays on that surface with the catalog id.
    assert mcp_err["repair"]["next_step"] == cli_err["repair"]["next_step"]
    assert mcp_err["repair"]["tool"] == "cwms_describe_place"
    assert mcp_err["repair"]["arguments"] == {
        "office": "SWT",
        "name": "FOSS",
        "detail": "summary",
    }
    assert cli_err["repair"]["tool"] == "cwms-tools place describe"
    assert cli_err["repair"]["arguments"] == {"detail": "summary", "spec": "SWT/FOSS"}


def test_describe_place_404_with_catalog_listing_name_keeps_original_error(
    configured, mocked
) -> None:
    _foss_catalogs(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", json={"detail": "nope"}, status=404)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "FOSS")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert ex.value.repair_intent is None
    assert "did you mean" not in ex.value.envelope.message


def test_describe_place_404_with_failing_catalog_keeps_original_not_found(
    configured, mocked
) -> None:
    _foss_catalogs(mocked, locations_status=503)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json={"detail": "nope"}, status=404)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    # The catalog could not confirm a spelling, so no same-tool retry is offered.
    assert ex.value.repair_intent is None


def test_describe_place_404_not_found_carries_endpoint_trail(configured, mocked) -> None:
    _arm_all(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json={"detail": "nope"}, status=404)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "Foss")
    assert ex.value.envelope.source.endpoints_called == ["/locations/Foss", "/catalog/LOCATIONS"]


def test_describe_place_wrong_case_not_found_skips_project_fetch(configured, mocked) -> None:
    """The existence check runs before the project read: a wrong-case id stops
    after /locations, the scoped ts catalog, and the locations catalog."""
    _foss_catalogs(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json=LOCATION_SINGLE_PAYLOAD, status=200)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert not any("/projects/" in c.request.url for c in mocked.calls)
    assert ex.value.envelope.source.endpoints_called == [
        "/locations/Foss",
        "/catalog/TIMESERIES",
        "/catalog/LOCATIONS",
    ]


def test_describe_place_ghost_degrades_when_locations_catalog_fails(configured, mocked) -> None:
    """#170: the existence check is part of the ghost enrichment; with
    /catalog/LOCATIONS down the describe still succeeds, `data_at` is null,
    and no office-wide enrichment is attempted (it needs the same catalog)."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"detail": "x"}, status=503)
    mocked.add(
        responses.GET,
        re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
        json={"entries": []},
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/GARR-Garrison_Dam",
        json={"office-id": "NWDM", "name": "GARR-Garrison_Dam"},
        status=200,
    )
    mocked.add(
        responses.GET, f"{API_ROOT}projects/GARR-Garrison_Dam", json={"detail": "no"}, status=404
    )
    payload = places.describe_place("NWDM", "GARR-Garrison_Dam")
    assert payload["data_at"] is None
    assert payload["partial"] is True
    assert payload["partial_reasons"] == ["not_a_project", "data_at_lookup_failed: upstream_error"]
    ts_urls = [c.request.url for c in mocked.calls if "catalog/TIMESERIES" in c.request.url]
    assert ts_urls and all("like=" in u.replace("group-like=", "") for u in ts_urls), (
        "no office-wide ts-catalog enrichment after the locations catalog failed"
    )


def test_describe_place_ghost_sibling_lookup_failure_degrades(configured, mocked) -> None:
    locs = {
        "locations": [
            {
                "office-id": "NWDM",
                "name": "GARR-Garrison_Dam",
                "latitude": 47.5,
                "longitude": -101.4,
            },
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locs, status=200)
    # Per-location ts read succeeds empty; the office-wide enrichment read fails.
    for _ in range(2):  # ts_ids_for_location, then freshness_for_location
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
            json={"entries": []},
            status=200,
        )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"detail": "x"}, status=503)
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/GARR-Garrison_Dam",
        json={"office-id": "NWDM", "name": "GARR-Garrison_Dam"},
        status=200,
    )
    mocked.add(
        responses.GET, f"{API_ROOT}projects/GARR-Garrison_Dam", json={"detail": "no"}, status=404
    )
    payload = places.describe_place("NWDM", "GARR-Garrison_Dam")
    assert payload["data_at"] is None
    assert payload["partial"] is True
    assert payload["partial_reasons"] == ["not_a_project", "data_at_lookup_failed: upstream_error"]


# --- #187: curated scope beyond NWD/MVD/SWD/LRD, ghost-only widening, scope_note


def _del_valle_catalogs(mocked) -> None:
    """Per-office catalogs: SPL holds a barren "Del Valle" record, SPK the
    data-bearing project (the live layout reported in #187); every other
    office is empty."""
    import json as _json
    from urllib.parse import parse_qs, urlparse

    def office_of(request) -> str:
        return parse_qs(urlparse(request.url).query).get("office", [""])[0]

    def locations(request):
        rows = {
            "SPL": [{"office-id": "SPL", "name": "Del Valle", "location-kind": "SITE"}],
            "SPK": [
                {
                    "office-id": "SPK",
                    "name": "Del Valle",
                    "public-name": "Del Valle Dam & Reservoir",
                    "location-kind": "PROJECT",
                }
            ],
        }.get(office_of(request), [])
        return (200, {}, _json.dumps({"locations": rows}))

    def timeseries(request):
        rows = (
            [{"name": "Del Valle.Elev.Inst.1Hour.0.CCP-Rev"}] if office_of(request) == "SPK" else []
        )
        return (200, {}, _json.dumps({"entries": rows}))

    mocked.add_callback(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        callback=locations,
        content_type="application/json",
    )
    mocked.add_callback(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        callback=timeseries,
        content_type="application/json",
    )


def _follow(hint: dict, *, max_hops: int = 12) -> list[dict]:
    """Follow a `repair_hint` chain to its end; fail if it does not terminate."""
    hops: list[dict] = []
    for _ in range(max_hops):
        resp = places.search_places(**hint["args"])
        hops.append(resp)
        if "repair_hint" not in resp:
            return hops
        hint = resp["repair_hint"]
    pytest.fail(f"repair_hint chain did not end within {max_hops} hops")


def test_discovery_candidates_include_the_surveyed_spd_sad_nad_districts() -> None:
    """#187: name-first discovery must reach SPD/SAD/NAD offices that publish."""
    cands = offices.discovery_office_candidates()
    for office in ("SPK", "SAM", "NAE", "SPL", "NAB", "SPA", "SAW", "SAJ", "SAS", "NAP", "NAO"):
        assert office in cands
    assert cands[:7] == ["NWDM", "NWDP", "SWT", "MVS", "SPK", "SAM", "NAE"]
    assert len(cands) == len(set(cands))


def test_search_places_cold_chain_reaches_spk_for_del_valle(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#187: following the hints from a cold cache must find SPK's Del Valle."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 4)
    _del_valle_catalogs(mocked)
    first = places.search_places("Del Valle")
    hops = _follow(first["repair_hint"])
    found = {(r["office_id"], r["name"], r["parameter_count"]) for h in hops for r in h["results"]}
    assert ("SPK", "Del Valle", 1) in found
    assert "SPK" in hops[1]["offices_searched"], "SPK is reached on the second hop"


def test_search_places_ghost_only_warm_match_still_widens(configured, mocked) -> None:
    """#187: a warm SPL catalog matching only a barren record must not stop
    the implicit search from naming the curated offices never searched."""
    _del_valle_catalogs(mocked)
    places.search_places("Del Valle", office="SPL")  # warms SPL only
    payload = places.search_places("Del Valle")
    assert payload["offices_searched"] == ["SPL"]
    assert [(r["office_id"], r["parameter_count"]) for r in payload["results"]] == [("SPL", 0)]
    assert payload["partial"] is True
    hint = payload["repair_hint"]
    assert hint["reason"] == "cached_scope_incomplete"
    assert "SPK" in hint["args"]["office"] and "SPL" not in hint["args"]["office"]
    assert "scope_note" not in payload
    hops = _follow(hint)
    assert any(
        r["office_id"] == "SPK" and r["parameter_count"] > 0 for h in hops for r in h["results"]
    )


def test_search_places_data_at_only_match_counts_as_actionable(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A barren row whose `data_at` names a publishing sibling is a usable answer."""
    rows = [
        {
            "office_id": "SWT",
            "name": "AAA",
            "parameter_count": 0,
            "publishers": [],
            "co_located": [],
            "data_at": [],
        },
        {
            "office_id": "SWT",
            "name": "BBB",
            "parameter_count": 0,
            "publishers": [],
            "co_located": ["BBB-D1"],
            "data_at": ["BBB-D1"],
        },
    ]
    monkeypatch.setattr(places.offices, "cached_offices_for_locations", lambda: ["SWT"])
    monkeypatch.setattr(
        places, "_gather_enriched", lambda *a, **k: ([dict(r) for r in rows], [], [])
    )
    monkeypatch.setattr(places, "_apply_parameter_filter", lambda rows, *a, **k: (rows, 0))
    # limit=1: the actionable row is on page 2; the predicate spans every match.
    payload = places.search_places("x", limit=1)
    assert [r["name"] for r in payload["results"]] == ["AAA"]
    assert payload["has_more"] is True
    assert "repair_hint" not in payload
    assert "scope_note" not in payload


def test_search_places_parameter_filtered_miss_widens(configured, mocked) -> None:
    """A warm match that does not publish `parameter` is filtered out, so the
    implicit search still names the curated offices never searched."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES_PAYLOAD, status=200)
    places.search_places("zzz-warmup", office="SWT")
    payload = places.search_places("FOSS", parameter="Temp-Water")
    assert payload["results"] == []
    assert payload["repair_hint"]["reason"] == "cached_scope_incomplete"
    assert payload["repair_hint"]["args"]["parameter"] == "Temp-Water"


def test_search_places_terminal_miss_carries_scope_note(configured, mocked) -> None:
    """#187: a response with no usable match and no `repair_hint` must not read
    as 'not in CWMS': it names the scope it covered, without a continuation."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    payload = places.search_places("Folsom", office=["SWT", "MVS"])
    assert "repair_hint" not in payload
    assert "partial" not in payload, "scope_note does not change partial semantics"
    note = payload["scope_note"]
    assert note["reason"] == "searched_offices_only"
    assert note["tool"] == "cwms_list_offices"
    assert "offices_searched" in note["message"]
    assert "cwms://offices" in note["message"]


def test_search_places_scope_note_absent_when_a_hint_or_match_exists(configured, mocked) -> None:
    _arm_all(mocked)
    hit = places.search_places("FOSS", office="SWT")
    assert hit["results"] and "scope_note" not in hit
    cold = places.search_places("Folsom")  # empty implicit scope: hint, no note
    assert "repair_hint" in cold and "scope_note" not in cold
    nothing = places.search_places("Folsom", office=[])  # deliberate empty scope
    assert "scope_note" not in nothing


def test_search_places_cursor_page_never_carries_scope_note(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {
            "office_id": "SWT",
            "name": n,
            "parameter_count": 0,
            "publishers": [],
            "co_located": [],
            "data_at": [],
        }
        for n in ("A", "B")
    ]
    monkeypatch.setattr(
        places, "_gather_enriched", lambda *a, **k: ([dict(r) for r in rows], [], [])
    )
    monkeypatch.setattr(places, "_apply_parameter_filter", lambda rows, *a, **k: (rows, 0))
    first = places.search_places("x", office="SWT", limit=1)
    assert first["scope_note"]["reason"] == "searched_offices_only"
    second = places.search_places("x", office="SWT", limit=1, cursor=first["next_cursor"])
    assert "scope_note" not in second and "repair_hint" not in second


# --------------------------------------------------------------------------
# #190: unenriched matches are unknown, never ghosts
# --------------------------------------------------------------------------


def _row(name: str, *, count: int = 0, incomplete: str | None = None, **extra) -> dict:
    row = {
        "office_id": "SAM",
        "name": name,
        "parameter_count": count,
        "parameters": ["Elev"] if count else [],
        "publishers": [],
        "co_located": [],
    }
    if incomplete:
        row["enrichment_incomplete"] = incomplete
    row.update(extra)
    return row


def _gather_returning(monkeypatch, rows_by_call):
    """Patch the fan-out to SAM and `_gather_enriched` to return each list in turn."""
    monkeypatch.setattr(places, "_run_fanout", lambda req, **kw: (["SAM"], [], []))
    calls = iter(rows_by_call)
    monkeypatch.setattr(
        places,
        "_gather_enriched",
        lambda offices, q, use_cache, reraise_single: ([dict(r) for r in next(calls)], [], []),
    )


def test_search_places_surfaces_unenriched_rows_as_partial(monkeypatch):
    err = {"code": "upstream_error", "temporary": True}
    rows = [
        _row("Carters", count=1),
        _row("Carters-Pool", incomplete="upstream_error", enrichment_error=err),
        _row("Carters-Tail", incomplete="upstream_error", enrichment_error=err),
        _row("Carters-LVDT#9", incomplete="batch_limit"),
    ]
    _gather_returning(monkeypatch, [rows])
    out = places.search_places("Carters", office="SAM")
    assert out["partial"] is True
    assert out["partial_reasons"] == [
        "SAM: enrichment_incomplete (3 of 4 matches not checked for data — "
        "2 upstream_error (temporary): search again; "
        "1 batch_limit: too many matches to enrich; narrow the query)"
    ]
    by_name = {r["name"]: r for r in out["results"]}
    assert by_name["Carters-Pool"]["enrichment_incomplete"] == "upstream_error"
    assert by_name["Carters"]["enrichment_incomplete"] is None
    assert "enrichment_error" not in by_name["Carters-Pool"]


def test_search_places_rate_limited_enrichment_reason_carries_the_wait(monkeypatch):
    err = {"code": "rate_limited", "temporary": True, "retry_after_ms": 7000}
    _gather_returning(
        monkeypatch,
        [[_row("Carters", incomplete="upstream_error", enrichment_error=err)]],
    )
    (reason,) = places.search_places("Carters", office="SAM")["partial_reasons"]
    assert "1 rate_limited (temporary): search again after 7000 ms" in reason


def test_search_places_permanent_enrichment_error_is_not_advised_as_retry(monkeypatch):
    err = {"code": "upstream_error", "temporary": False}
    _gather_returning(
        monkeypatch,
        [[_row("Carters", incomplete="upstream_error", enrichment_error=err)]],
    )
    out = places.search_places("Carters", office="SAM")
    (reason,) = out["partial_reasons"]
    assert "search again" not in reason
    assert "upstream_error: use cwms_list_parameters" in reason


def test_search_places_parameter_filter_keeps_unenriched_rows(monkeypatch):
    """An unknown row may publish the parameter: keep it, don't count it as a
    non-match, and don't fetch the whole office to repair it."""
    _gather_returning(
        monkeypatch,
        [[_row("Carters", count=1), _row("Carters-Pool", incomplete="batch_limit")]],
    )

    def _no_broader(*a, **kw):
        raise AssertionError("an unenriched row must not trigger the broader fetch")

    monkeypatch.setattr(places.catalog, "enrich_locations", _no_broader)
    out = places.search_places("Carters", office="SAM", parameter="Elev")
    assert {r["name"] for r in out["results"]} == {"Carters", "Carters-Pool"}
    assert out["nearby_non_matching_count"] == 0


def test_search_places_unenriched_rows_suppress_scope_note(monkeypatch):
    """ "No match has data" is unproven while a match is unchecked."""
    monkeypatch.setattr(places.catalog, "enrich_locations", lambda *a, **kw: [])
    _gather_returning(monkeypatch, [[_row("Carters-Pool", incomplete="upstream_error")]])
    assert "scope_note" not in places.search_places("Carters", office="SAM")

    _gather_returning(monkeypatch, [[_row("Carters-Pool")]])  # control: a real ghost
    assert "scope_note" in places.search_places("Carters", office="SAM")


def test_search_places_cursor_rejects_reorder_with_same_total(monkeypatch):
    """Page 1 saw B unenriched (sorted last); recovery moves B first with the
    same total. Continuing by offset would repeat A and skip B."""
    before = [_row("A", count=1), _row("B", incomplete="upstream_error"), _row("C")]
    after = [_row("A", count=1), _row("B", count=2), _row("C")]
    _gather_returning(monkeypatch, [before, after])
    page1 = places.search_places("x", office="SAM", limit=1)
    assert [r["name"] for r in page1["results"]] == ["A"]
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("x", office="SAM", limit=1, cursor=page1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_cursor_without_order_is_rejected(monkeypatch):
    rows = [_row("A", count=1), _row("B", count=1)]
    _gather_returning(monkeypatch, [rows, rows])
    page1 = places.search_places("x", office="SAM", limit=1)
    legacy = pagination.decode_cursor(page1["next_cursor"])
    del legacy["order"]
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("x", office="SAM", limit=1, cursor=pagination.encode_cursor(legacy))
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_single_office_degrades_on_enrichment_500(configured, mocked):
    """#190 end to end: a 500 on the ts-catalog lookup used to fail an explicit
    single-office search with a temporary upstream_error; the rows now return."""
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json={
            "entries": [
                {"office": "SAM", "name": "Carters", "latitude": 34.6, "longitude": -84.7},
                {"office": "SAM", "name": "Carters-Pool", "latitude": 34.6, "longitude": -84.6},
            ]
        },
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", status=500)
    out = places.search_places("Carters", office="SAM", use_cache=False)
    assert [r["name"] for r in out["results"]] == ["Carters", "Carters-Pool"]
    assert all(r["enrichment_incomplete"] == "upstream_error" for r in out["results"])
    assert out["partial"] is True
    assert "(temporary): search again" in out["partial_reasons"][0]


def test_search_places_match_mid_chain_turns_the_hint_optional_then_back(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#191's Folsom run: hop 2 finds SPK data, so its hint is optional; an agent
    that follows it anyway for completeness gets the mandatory reason back on a
    hop with no match, and the chain still ends with no hint."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 4)
    folsom = {
        "office_id": "SPK",
        "name": "FOL",
        "parameter_count": 12,
        "publishers": ["Rev"],
        "co_located": [],
        "data_at": [],
    }

    def gather(office_ids, query, **_):
        return ([dict(folsom)] if "SPK" in office_ids else []), [], []

    monkeypatch.setattr(places, "_gather_enriched", gather)
    monkeypatch.setattr(places, "_apply_parameter_filter", lambda rows, *a, **k: (rows, 0))
    hint = places.search_places("Folsom")["repair_hint"]
    reasons: list[str] = []
    for _ in range(10):
        resp = places.search_places(**hint["args"])
        if "repair_hint" not in resp:
            break
        hint = resp["repair_hint"]
        reasons.append(hint["reason"])
        if "SPK" in resp["offices_searched"]:
            assert hint["reason"] == "match_found_offices_skipped"
        else:
            assert hint["reason"] == "offices_skipped_for_budget"
    assert reasons[0] == "offices_skipped_for_budget"
    assert reasons[1] == "match_found_offices_skipped"
    assert set(reasons[2:]) == {"offices_skipped_for_budget"}


def test_search_places_later_page_match_still_makes_the_budget_hint_optional(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The predicate spans every match, so a usable row on page 2 counts; the
    message points at `next_cursor` rather than claiming it is on this page."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    rows = [
        {
            "office_id": "SWT",
            "name": "AAA",
            "parameter_count": 0,
            "publishers": [],
            "co_located": [],
            "data_at": [],
        },
        {
            "office_id": "SWT",
            "name": "BBB",
            "parameter_count": 0,
            "publishers": [],
            "co_located": ["BBB-D1"],
            "data_at": ["BBB-D1"],
        },
    ]
    monkeypatch.setattr(
        places, "_gather_enriched", lambda *a, **k: ([dict(r) for r in rows], [], [])
    )
    monkeypatch.setattr(places, "_apply_parameter_filter", lambda rows, *a, **k: (rows, 0))
    payload = places.search_places("x", office=["SWT", "MVS"], limit=1)
    assert [r["name"] for r in payload["results"]] == ["AAA"] and payload["has_more"]
    hint = payload["repair_hint"]
    assert hint["reason"] == "match_found_offices_skipped"
    assert hint["args"] == {"query": "x", "office": ["MVS"]}
    assert "`next_cursor`" in hint["message"]


def test_search_repair_hint_schema_marks_the_match_found_reason_optional() -> None:
    """#191 review: the machine-visible contract must not say "always continue"."""
    from cwms_tools.core.models import SearchPlacesResponse

    desc = SearchPlacesResponse.model_fields["repair_hint"].description or ""
    assert "continue unless `match_found_offices_skipped` (optional)" in desc
    assert "continue while one is present" not in desc
    # Copilot review: `reason` is the branch key, so the schema advertises its values.
    reason = SearchPlacesResponse.model_json_schema()["$defs"]["SearchRepairHint"]["properties"][
        "reason"
    ]
    assert reason["enum"] == [
        "no_offices_in_scope",
        "offices_skipped_for_budget",
        "cached_scope_incomplete",
        "match_found_offices_skipped",
    ]


# --------------------------------------------------------------------------
# state filter (#188)
# --------------------------------------------------------------------------


def _capture_fanout(monkeypatch: pytest.MonkeyPatch, *, budget: int = 99) -> list[list[str]]:
    """Stub the fan-out (searching the first `budget` offices) and gather no
    rows; return the office list each call asked for."""
    asked: list[list[str]] = []

    def fanout(requested, *, use_cache, explicit_empty):
        asked.append(list(requested))
        searched, skipped = requested[:budget], requested[budget:]
        reasons = [f"offices_skipped_for_budget: {len(skipped)}"] if skipped else []
        return list(searched), list(skipped), reasons

    monkeypatch.setattr(places, "_run_fanout", fanout)
    monkeypatch.setattr(
        places, "_gather_enriched", lambda offices, q, use_cache, reraise_single: ([], [], [])
    )
    return asked


def test_search_places_state_searches_the_covering_offices(monkeypatch) -> None:
    asked = _capture_fanout(monkeypatch)
    payload = places.search_places("Del Valle", state="ca")
    assert asked == [["SPK", "SPL", "SPN"]]
    assert payload["state"] == "CA"
    assert payload["office"] is None
    assert payload["offices_searched"] == ["SPK", "SPL", "SPN"]
    # An exhausted state scope is a finished miss, not proof of absence.
    assert "repair_hint" not in payload
    assert payload["scope_note"]["reason"] == "searched_offices_only"


def test_search_places_without_state_has_no_state_echo(monkeypatch) -> None:
    _capture_fanout(monkeypatch)
    assert "state" not in places.search_places("Del Valle", office="SPK")


def test_search_places_state_budget_chain_stays_inside_the_state(monkeypatch) -> None:
    """Budget hints carry only the remaining state offices, as `office`
    without `state`, and the chain ends once they are all searched."""
    asked = _capture_fanout(monkeypatch, budget=2)
    first = places.search_places("Lake", state="TX", parameter="Elev")
    assert first["offices_searched"] == ["SWT", "SWG"]
    hint = first["repair_hint"]
    assert hint["reason"] == "offices_skipped_for_budget"
    assert hint["args"] == {"query": "Lake", "office": ["SWF", "SPA"], "parameter": "Elev"}
    second = places.search_places(**hint["args"])
    assert second["offices_searched"] == ["SWF", "SPA"]
    assert "repair_hint" not in second
    assert asked == [["SWT", "SWG", "SWF", "SPA"], ["SWF", "SPA"]]


@pytest.mark.parametrize("office", ["SPK", ["SPK", "SPL"], []])
def test_search_places_state_with_office_is_a_usage_error(monkeypatch, office) -> None:
    asked = _capture_fanout(monkeypatch)
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("Del Valle", state="CA", office=office)
    assert ex.value.envelope.code is ErrorCode.USAGE_ERROR
    assert asked == [], "rejected before any fan-out"


def test_search_places_unknown_state_is_invalid_field_before_fanout(monkeypatch) -> None:
    asked = _capture_fanout(monkeypatch)
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("Del Valle", state="ZZ")
    env = ex.value.envelope
    assert env.code is ErrorCode.INVALID_FIELD and env.details is not None
    assert env.details.field == "state"
    assert asked == []


def _state_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "office_id": "SPK",
            "name": f"L{i}",
            "parameter_count": 1,
            "parameters": ["Elev"],
            "publishers": [],
            "co_located": [],
        }
        for i in range(3)
    ]
    monkeypatch.setattr(places, "_run_fanout", lambda req, **_: (list(req), [], []))
    monkeypatch.setattr(
        places,
        "_gather_enriched",
        lambda offices, q, use_cache, reraise_single: (list(rows), [], []),
    )


def test_search_places_state_cursor_continues_and_matches_case_insensitively(
    monkeypatch,
) -> None:
    _state_rows(monkeypatch)
    page1 = places.search_places("L", state="CA", limit=2)
    page2 = places.search_places("L", state="ca", limit=2, cursor=page1["next_cursor"])
    assert [r["name"] for r in page2["results"]] == ["L2"]
    assert page2["state"] == "CA" and page2["offices_searched"] == ["SPK", "SPL", "SPN"]


@pytest.mark.parametrize("state", ["NV", None])
def test_search_places_state_cursor_rejects_a_changed_or_dropped_state(monkeypatch, state) -> None:
    _state_rows(monkeypatch)
    page1 = places.search_places("L", state="CA", limit=2)
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("L", state=state, limit=2, cursor=page1["next_cursor"])
    assert ex.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_without_state_keeps_the_pre_188_request_hash(monkeypatch) -> None:
    """Cursors minted before `state` existed must still validate."""
    _state_rows(monkeypatch)
    page1 = places.search_places("L", office="SPK", limit=2)
    decoded = pagination.decode_cursor(page1["next_cursor"])
    assert decoded["req"] == pagination.request_hash({"q": "L", "parameter": None})
