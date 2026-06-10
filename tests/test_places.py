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
    # The format-error workaround marker must NOT be reused for the 404 case.
    assert project_resp["source_workaround"] is None


def test_describe_place_falls_back_when_project_lookup_is_other_4xx(configured, mocked) -> None:
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


def test_describe_place_raises_upstream_error_on_project_5xx(configured, mocked) -> None:
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


def test_browse_region_limit_truncates_and_reports_total_count(configured, mocked) -> None:
    """M2/C4: browse caps results and reports the full size + a repair hint."""
    _arm_all(mocked)
    payload = places.browse_region(office="SWT", limit=1)
    assert payload["result_count"] == 1
    assert payload["total_count"] == 2
    assert payload["truncated"] is True
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
    """A single NW-stub office must surface the ghost_office envelope, not empty results."""
    with pytest.raises(CwmsToolsError) as exc_info:
        places.search_places("Bear Creek", office="NWO")
    env = exc_info.value.envelope
    assert env.code is ErrorCode.GHOST_OFFICE
    assert env.repair is not None
    # catalog._raise_ghost_office points at cwms_browse_region.
    # TODO(Task 2): becomes cwms_browse_region consistently (already is from catalog path)
    assert env.repair.tool == "cwms_browse_region"


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


# --------------------------------------------------------------------------
# search_places --limit truncation
# --------------------------------------------------------------------------


def test_search_places_caps_result_count_by_default(configured, mocked) -> None:
    """Broad searches should be capped so agents don't get flooded.
    Default cap is 50; rows past the cap are dropped and the response
    carries `truncated: true` plus the full `total_count`."""
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
    assert payload["truncated"] is True
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

    def _counting_fanout(req):
        nonlocal fanout_calls
        fanout_calls += 1
        return (["NWDM"], [], [])

    monkeypatch.setattr(places, "_run_fanout", _counting_fanout)
    monkeypatch.setattr(places, "_gather_enriched", lambda offices, q, use_cache: (list(rows), []))

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

    assert fanout_calls == 1  # only page 1 fans out; pages 2-3 use the locked cursor


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
    monkeypatch.setattr(places, "_run_fanout", lambda req: (["NWDM"], [], []))
    calls = {"n": 0}

    def _shifting_gather(offices, q, use_cache):
        calls["n"] += 1
        return (list(five if calls["n"] == 1 else six), [])  # catalog grows between calls

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
    monkeypatch.setattr(places, "_run_fanout", lambda req: (["NWDM"], [], []))
    monkeypatch.setattr(places, "_gather_enriched", lambda offices, q, use_cache: (list(rows), []))
    page1 = places.search_places("L", office="NWDM", limit=2)
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("DIFFERENT", office="NWDM", limit=2, cursor=page1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_search_places_rejects_cursor_with_unlimited_limit(monkeypatch):
    # A cursor combined with an unlimited limit (0 -> None) is contradictory and
    # must be rejected before any fan-out, not silently return a tail subset.
    monkeypatch.setattr(places, "_run_fanout", lambda req: (["NWDM"], [], []))
    monkeypatch.setattr(places, "_gather_enriched", lambda offices, q, use_cache: ([], []))
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("L", office="NWDM", limit=0, cursor="anytoken")
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
