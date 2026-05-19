"""Tests for the catalog enrichment layer.

Mocks `cwms-python`'s `requests`-based CDA traffic via the `responses`
library. Verifies ghost detection, NW-stub repair hints, and the per-record
enrichment shape (`parameter_count`, `publishers`, `co_located`, `freshness`).
"""

from __future__ import annotations

import cwms
import pytest
import responses

from cwms_tools.core import catalog, session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode

API_ROOT = "https://example.test/cwms-data/"


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Configure session against `example.test` and isolate cache to tmp_path."""
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


# --------------------------------------------------------------------------
# Fixture payloads — minimal shapes covering Foss happy / NWO ghost.
# --------------------------------------------------------------------------

SWT_LOCATIONS_PAYLOAD = {
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
            "name": "FOSS-bl_1500",
            "public-name": "Foss Reservoir below 1500ft",
            "location-kind": "SITE",
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

SWT_TIMESERIES_PAYLOAD = {
    "entries": [
        {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", "last-update": "2026-05-17T18:00:00Z"},
        {"name": "FOSS.Flow-Out.Inst.15Minutes.0.Ccp-Rev", "last-update": "2026-05-17T17:45:00Z"},
        {"name": "FOSS.Elev.Inst.15Minutes.0.Raw-A2W", "last-update": "2026-05-17T17:00:00Z"},
    ],
    # FOSS-bl_1500 and CHOU-Lock intentionally absent — they are ghosts.
}


def _arm(mocked, *, locations_payload=None, timeseries_payload=None, office: str = "SWT"):
    if locations_payload is not None:
        mocked.add(
            method=responses.GET,
            url=f"{API_ROOT}catalog/LOCATIONS",
            json=locations_payload,
            status=200,
        )
    if timeseries_payload is not None:
        mocked.add(
            method=responses.GET,
            url=f"{API_ROOT}catalog/TIMESERIES",
            json=timeseries_payload,
            status=200,
        )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_get_locations_catalog_caches_responses(configured, mocked) -> None:
    _arm(mocked, locations_payload=SWT_LOCATIONS_PAYLOAD)
    first = catalog.get_locations_catalog("SWT")
    assert "locations" in first
    # Second call must come from cache — no new HTTP traffic.
    second = catalog.get_locations_catalog("SWT")
    assert second == first
    assert len(mocked.calls) == 1


def test_get_locations_catalog_refetches_when_use_cache_false(configured, mocked) -> None:
    _arm(mocked, locations_payload=SWT_LOCATIONS_PAYLOAD)
    _arm(mocked, locations_payload=SWT_LOCATIONS_PAYLOAD)
    catalog.get_locations_catalog("SWT")
    catalog.get_locations_catalog("SWT", use_cache=False)
    assert len(mocked.calls) == 2


def test_nw_district_office_raises_ghost_office_with_repair(configured) -> None:
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_locations_catalog("NWO")
    err = ex_info.value.envelope
    assert err.code is ErrorCode.GHOST_OFFICE
    assert err.field == "office_id"
    assert err.offending_value == "NWO"
    assert err.repair is not None
    assert err.repair.tool == "cwms_browse_region"
    assert err.repair.args["office"] in {"NWDM", "NWDP"}


def test_enrich_locations_marks_ghost_records(configured, mocked) -> None:
    _arm(
        mocked,
        locations_payload=SWT_LOCATIONS_PAYLOAD,
        timeseries_payload=SWT_TIMESERIES_PAYLOAD,
    )
    rows = catalog.enrich_locations("SWT")
    names = {r["name"]: r for r in rows}
    # FOSS is data-bearing.
    assert names["FOSS"]["parameter_count"] == 2  # Elev + Flow-Out
    assert "Ccp-Rev" in names["FOSS"]["publishers"]
    assert "Raw-A2W" in names["FOSS"]["publishers"]
    # CHOU-Lock is a ghost.
    assert names["CHOU-Lock"]["parameter_count"] == 0
    assert names["CHOU-Lock"]["publishers"] == []


def test_enrich_locations_detects_co_located_siblings(configured, mocked) -> None:
    _arm(
        mocked,
        locations_payload=SWT_LOCATIONS_PAYLOAD,
        timeseries_payload=SWT_TIMESERIES_PAYLOAD,
    )
    rows = catalog.enrich_locations("SWT")
    foss = next(r for r in rows if r["name"] == "FOSS")
    # FOSS and FOSS-bl_1500 share coordinates.
    assert "FOSS-bl_1500" in foss["co_located"]


def test_enrich_locations_carries_last_data_timestamp(configured, mocked) -> None:
    _arm(
        mocked,
        locations_payload=SWT_LOCATIONS_PAYLOAD,
        timeseries_payload=SWT_TIMESERIES_PAYLOAD,
    )
    rows = catalog.enrich_locations("SWT")
    foss = next(r for r in rows if r["name"] == "FOSS")
    assert foss["last_data_timestamp"] == "2026-05-17T18:00:00Z"


def test_ts_ids_for_location_filters_by_prefix(configured, mocked) -> None:
    _arm(mocked, timeseries_payload=SWT_TIMESERIES_PAYLOAD)
    tsids = catalog.ts_ids_for_location("SWT", "FOSS")
    assert all(t.startswith("FOSS.") for t in tsids)
    assert len(tsids) == 3


def test_enrich_locations_scopes_ts_catalog_query_to_matched_names(configured, mocked) -> None:
    """When a `like` filter is in play, the ts catalog query carries a regex
    that restricts the response to ts_ids whose location segment is one of
    the matched names — avoiding a multi-megabyte fetch of an office's full
    ts catalog on what should be a tight name search."""
    _arm(
        mocked,
        locations_payload=SWT_LOCATIONS_PAYLOAD,
        timeseries_payload=SWT_TIMESERIES_PAYLOAD,
    )
    catalog.enrich_locations("SWT", like="FOSS")
    ts_calls = [c for c in mocked.calls if "catalog/TIMESERIES" in c.request.url]
    assert ts_calls, "ts catalog should be queried"
    from urllib.parse import unquote

    url = unquote(ts_calls[-1].request.url)
    # The matched names (FOSS, FOSS-bl_1500) appear as an anchored,
    # alternation regex prefix: `like=^(FOSS|FOSS\-bl_1500)\.`.
    assert "like=^(" in url
    assert "FOSS" in url
    assert r"FOSS\-bl_1500" in url  # re.escape adds the backslash


def test_enrich_locations_matches_against_public_name_not_just_id(configured, mocked) -> None:
    """A search for the human-readable public-name should resolve to the
    location even when its canonical id is an abbreviation. The original
    bug: `place search "Fort Peck" --office NWDM` returned zero results
    because CDA's server-side `like` only matched the id "FTPK", not the
    public-name "Fort Peck Lake"."""
    nwdm_locations = {
        "locations": [
            {
                "office-id": "NWDM",
                "name": "FTPK",
                "public-name": "Fort Peck Lake",
                "long-name": "Fort Peck Lake, MT",
                "location-kind": "PROJECT",
                "latitude": 47.991,
                "longitude": -106.412,
            }
        ],
    }
    nwdm_timeseries = {
        "entries": [
            {"name": "FTPK.Elev.Inst.1Hour.0.Best-MRBWM"},
        ]
    }
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        json=nwdm_locations,
        status=200,
    )
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        json=nwdm_timeseries,
        status=200,
    )
    rows = catalog.enrich_locations("NWDM", like="Fort Peck")
    assert len(rows) == 1
    assert rows[0]["name"] == "FTPK"
    assert rows[0]["public_name"] == "Fort Peck Lake"


def test_enrich_locations_does_not_pass_like_to_server_side_locations_catalog(
    configured, mocked
) -> None:
    """The locations catalog request is unfiltered — the `like` parameter
    is applied client-side because CDA's server-side filter only matches
    the location id, missing public-name lookups."""
    _arm(
        mocked,
        locations_payload=SWT_LOCATIONS_PAYLOAD,
        timeseries_payload=SWT_TIMESERIES_PAYLOAD,
    )
    catalog.enrich_locations("SWT", like="Foss Reservoir")
    loc_calls = [c for c in mocked.calls if "catalog/LOCATIONS" in c.request.url]
    assert loc_calls, "locations catalog should be queried"
    # No `like=` parameter on the locations catalog URL.
    assert "like=" not in str(loc_calls[-1].request.url)


def test_enrich_locations_dedupes_repeated_rows_from_upstream(configured, mocked) -> None:
    """The upstream `/catalog/LOCATIONS` returns multiple rows per `name`
    (different bounding-office / alias variants). Enrichment must collapse
    those into one entry per name so the response doesn't carry obvious
    duplicates."""
    duplicated = {
        "entries": [
            {
                "office-id": "SWT",
                "name": "FOSS",
                "public-name": "Foss Reservoir",
                "latitude": 35.55,
                "longitude": -98.97,
            },
            {
                "office-id": "SWT",
                "name": "FOSS",
                "public-name": "Foss Reservoir",
                "latitude": 35.55,
                "longitude": -98.97,
            },
            {
                "office-id": "SWT",
                "name": "FOSS",
                "public-name": "Foss Reservoir",
                "latitude": 35.55,
                "longitude": -98.97,
            },
        ]
    }
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        json=duplicated,
        status=200,
    )
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": []},
        status=200,
    )
    rows = catalog.enrich_locations("SWT", like="FOSS")
    assert len(rows) == 1
    assert rows[0]["name"] == "FOSS"


def test_enrich_locations_reads_latest_time_from_extents_array(configured, mocked) -> None:
    """When `include_extents=True`, ts catalog rows carry an `extents`
    array whose `latest-time` field is the freshness signal. The earlier
    code looked at top-level fields only and reported null."""
    locations = {
        "entries": [
            {
                "office-id": "SWT",
                "name": "FOSS",
                "latitude": 35.55,
                "longitude": -98.97,
            }
        ]
    }
    timeseries = {
        "entries": [
            {
                "name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                "extents": [
                    {
                        "earliest-time": "2000-01-01T00:00:00Z",
                        "latest-time": "2026-05-18T22:00:00Z",
                        "last-update": "2026-05-18T22:01:00Z",
                    }
                ],
            }
        ]
    }
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        json=locations,
        status=200,
    )
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        json=timeseries,
        status=200,
    )
    rows = catalog.enrich_locations("SWT", like="FOSS")
    assert rows[0]["last_data_timestamp"] == "2026-05-18T22:00:00Z"


def test_get_timeseries_catalog_omits_extents_by_default(configured, mocked) -> None:
    """Default is `include_extents=False`. Requesting extents on every ts
    catalog query inflates responses by 10-100x and made `value get`
    unusable. Callers that need freshness must opt in explicitly."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": []},
        status=200,
    )
    catalog.get_timeseries_catalog("SWT", like="^FOSS\\.")
    call_url = str(mocked.calls[-1].request.url)
    assert "include-extents=False" in call_url or "include_extents=False" in call_url


def test_get_timeseries_catalog_passes_include_extents_when_requested(configured, mocked) -> None:
    """Opt-in extents make their way through to the upstream call."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": []},
        status=200,
    )
    catalog.get_timeseries_catalog("SWT", like="^FOSS\\.", include_extents=True)
    call_url = str(mocked.calls[-1].request.url)
    assert "include-extents=True" in call_url or "include_extents=True" in call_url


def test_ts_ids_for_location_scopes_request_to_the_location(configured, mocked) -> None:
    """The ts ids lookup must NOT fetch the whole office's ts catalog —
    it scopes server-side to the matched location segment so a `value get`
    call doesn't pay multi-megabyte transfer cost up front."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": [{"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"}]},
        status=200,
    )
    catalog.ts_ids_for_location("SWT", "FOSS")
    from urllib.parse import unquote

    call_url = unquote(str(mocked.calls[-1].request.url))
    assert "like=^FOSS\\." in call_url


def test_enrich_locations_returns_empty_when_name_filter_matches_nothing(
    configured, mocked
) -> None:
    """If the name filter matches no locations, skip the ts catalog fetch
    entirely — there is nothing to enrich and the second round trip would be
    wasted."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        json={"locations": []},
        status=200,
    )
    out = catalog.enrich_locations("SWT", like="DefinitelyNotAPlaceName")
    assert out == []
    ts_calls = [c for c in mocked.calls if "catalog/TIMESERIES" in c.request.url]
    assert not ts_calls, "no ts catalog query should be made for an empty match set"


def test_enrich_locations_skips_ts_catalog_when_alternation_overflows(
    configured, mocked, monkeypatch
) -> None:
    """Broad searches with many matches would build a too-large alternation
    regex and CDA rejects the request with a 500. We must skip the ts catalog
    fetch and mark rows truncated rather than firing the oversized request."""
    # Tighten the byte budget so we don't need to generate thousands of fixture
    # rows to trigger the branch.
    monkeypatch.setattr(catalog, "MAX_TS_LIKE_BYTES", 64)
    locations_payload = {
        "locations": [
            {
                "office-id": "NWDP",
                "name": f"Bridge_{i:03d}",
                "public-name": f"Bridge #{i}",
                "latitude": 47.0,
                "longitude": -122.0,
            }
            for i in range(25)
        ]
    }
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        json=locations_payload,
        status=200,
    )
    rows = catalog.enrich_locations("NWDP", like="Bridge")
    assert len(rows) == 25
    assert all(r.get("enrichment_truncated") is True for r in rows)
    assert rows[0]["enrichment_truncated_reason"] == "alternation_overflow"
    ts_calls = [c for c in mocked.calls if "catalog/TIMESERIES" in c.request.url]
    assert not ts_calls, "the oversized regex must not hit the upstream"


def test_get_locations_catalog_wraps_5xx_as_retryable_upstream_error(configured, mocked) -> None:
    """Upstream 5xx errors must become a structured CwmsToolsError, not a
    bare ApiError traceback — that's the regression that broke broad
    `place search` in the eval."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        status=500,
        body="Internal Server Error",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_locations_catalog("SWT", use_cache=False)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.retryable is True
    assert env.endpoints_called == ["/catalog/LOCATIONS"]


def test_get_timeseries_catalog_wraps_404_as_not_found(configured, mocked) -> None:
    """A 404 from the ts catalog endpoint is non-retryable and surfaces as
    NOT_FOUND, not UPSTREAM_ERROR."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        status=404,
        body="Not Found",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_timeseries_catalog("SWT", like="^FOSS\\.", use_cache=False)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.NOT_FOUND
    assert env.retryable is False


def test_upstream_error_message_does_not_embed_long_url(configured, mocked) -> None:
    """The error envelope's `message` must stay compact even when the
    upstream request URL is large (e.g. a 3 KB alternation regex). The URL
    is implicit in `endpoints_called`; agents that need it can dig in."""
    huge_regex = "^(" + "|".join(f"Name_{i:04d}" for i in range(200)) + r")\."
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/TIMESERIES",
        status=500,
        body="Internal Server Error",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_timeseries_catalog("SWT", like=huge_regex, use_cache=False)
    env = ex_info.value.envelope
    # The message must NOT contain the alternation regex.
    assert "Name_0050" not in env.message
    assert "|" not in env.message
    # And it must stay reasonably compact — well under 200 chars.
    assert len(env.message) < 200
