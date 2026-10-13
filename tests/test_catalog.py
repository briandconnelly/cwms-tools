"""Tests for the catalog enrichment layer.

Mocks `cwms-python`'s `requests`-based CDA traffic via the `responses`
library. Verifies ghost detection, NW-stub repair hints, and the per-record
enrichment shape (`parameter_count`, `publishers`, `co_located`, `freshness`).
"""

from __future__ import annotations

import json

import cwms
import pytest
import responses

from cwms_tools.core import catalog, session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode

from .conftest import fixture_path

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


def test_nw_district_office_raises_ghost_office_no_repair_at_core_level(configured) -> None:
    """#69: core no longer hardcodes a same-tool-switching repair — a same-CALL
    retry needs the calling surface's own tool name and original arguments,
    which core doesn't have. `mcp.tools._safe`/`cli.render` attach the repair
    via `core.offices.ghost_office_repair` at the surface boundary instead."""
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_locations_catalog("NWO")
    err = ex_info.value.envelope
    assert err.code is ErrorCode.GHOST_OFFICE
    assert err.details is not None
    assert err.details.field == "office_id"
    assert err.details.value == "NWO"
    assert err.repair is None


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


def _bridge_locations(count: int, prefix: str = "Bridge_") -> dict:
    return {
        "locations": [
            {
                "office-id": "NWDP",
                "name": f"{prefix}{i:03d}",
                "public-name": f"Bridge #{i}",
                "latitude": 47.0 + i,
                "longitude": -122.0,
            }
            for i in range(count)
        ]
    }


_JSON = {"Content-Type": "application/json"}
# CDA's measured limit, independent of the constant under test (#190).
_CDA_LIKE_LIMIT = 512


def _ts_catalog_callback(fail_when=lambda like: False, status: int = 500):
    """Answer each ts-catalog batch with one Elev series per name it can match.

    Mirrors CDA: a `like` over 512 raw bytes is a 500, so an
    oversized request fails the test instead of passing silently.
    """
    import re
    from urllib.parse import parse_qs, urlparse

    def callback(request):
        like = parse_qs(urlparse(request.url).query)["like"][0]
        if len(like.encode("utf-8")) > _CDA_LIKE_LIMIT or fail_when(like):
            return (status, _JSON, json.dumps({"message": "Database Error"}))
        names = [re.sub(r"\\(.)", r"\1", n) for n in like[2:-3].split("|")]
        entries = [{"name": f"{n}.Elev.Inst.1Hour.0.CBT-RAW"} for n in names]
        return (200, _JSON, json.dumps({"entries": entries}))

    return callback


def _ts_likes(mocked) -> list[str]:
    from urllib.parse import parse_qs, urlparse

    return [
        parse_qs(urlparse(c.request.url).query)["like"][0]
        for c in mocked.calls
        if "catalog/TIMESERIES" in c.request.url
    ]


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], []),
        (["A", "B"], [["A", "B"]]),
        # Escaped metacharacters count at their escaped length.
        (["a#b", "c d"], [["a#b", "c d"]]),
    ],
)
def test_pack_ts_like_batches_small_inputs(names, expected) -> None:
    batches, oversize = catalog._pack_ts_like_batches(names)
    assert [b for _, b in batches] == expected
    assert oversize == []


def test_pack_ts_like_batches_fills_to_exactly_the_byte_limit() -> None:
    """512 bytes is allowed and 513 is not (CDA's live boundary, #190)."""
    overhead = len("^(|)\\.")  # anchors, parens, one separator, escaped dot
    first = "A" * 100
    fits = "B" * (catalog.MAX_TS_LIKE_BYTES - overhead - len(first))
    batches, _ = catalog._pack_ts_like_batches([first, fits])
    assert len(batches) == 1
    assert len(batches[0][0].encode()) == catalog.MAX_TS_LIKE_BYTES

    batches, _ = catalog._pack_ts_like_batches([first, fits + "C"])
    assert [b for _, b in batches] == [[first], [fits + "C"]]


def test_pack_ts_like_batches_measures_utf8_bytes_not_characters() -> None:
    """A two-byte character must count twice, or the batch overflows on CDA."""
    name = "é" * 150  # 150 chars, 300 bytes
    batches, _ = catalog._pack_ts_like_batches([name, name])
    assert [b for _, b in batches] == [[name], [name]]
    assert all(len(like.encode()) <= catalog.MAX_TS_LIKE_BYTES for like, _ in batches)


def test_pack_ts_like_batches_sets_oversize_name_aside_and_packs_the_rest() -> None:
    huge = "X" * catalog.MAX_TS_LIKE_BYTES
    batches, oversize = catalog._pack_ts_like_batches(["A", huge, "B"])
    assert oversize == [huge]
    assert [b for _, b in batches] == [["A", "B"]]


def test_enrich_locations_batches_like_under_the_byte_limit(configured, mocked) -> None:
    """#190: a match set whose single alternation would exceed 512 bytes is
    split into batches; every row is enriched and every request fits."""
    mocked.add(
        responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=_bridge_locations(60), status=200
    )
    mocked.add_callback(
        responses.GET, f"{API_ROOT}catalog/TIMESERIES", callback=_ts_catalog_callback()
    )
    rows = catalog.enrich_locations("NWDP", like="Bridge")
    assert len(rows) == 60
    assert all(r["parameter_count"] == 1 for r in rows)
    assert not any("enrichment_incomplete" in r for r in rows)
    likes = _ts_likes(mocked)
    assert len(likes) > 1
    assert all(len(like.encode()) <= catalog.MAX_TS_LIKE_BYTES for like in likes)


def test_enrich_locations_failed_batch_degrades_only_its_rows(configured, mocked) -> None:
    """A 500 on one batch must not fail the search or blank other batches."""
    mocked.add(
        responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=_bridge_locations(60), status=200
    )
    mocked.add_callback(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        callback=_ts_catalog_callback(fail_when=lambda like: "Bridge_000" in like),
    )
    rows = catalog.enrich_locations("NWDP", like="Bridge", use_cache=False)
    failed = [r for r in rows if r.get("enrichment_incomplete")]
    ok = [r for r in rows if not r.get("enrichment_incomplete")]
    assert failed and ok
    assert failed[0]["name"] == "Bridge_000"
    assert all(r["enrichment_incomplete"] == "upstream_error" for r in failed)
    assert failed[0]["enrichment_error"] == {
        "code": "upstream_error",
        "temporary": True,
        "retry_after_ms": None,
    }
    assert all(r["parameter_count"] == 0 for r in failed)
    assert all(r["parameter_count"] == 1 for r in ok)


def test_enrich_locations_stops_batches_on_rate_limit(configured, mocked) -> None:
    """A 429 must not be followed by the remaining batches; every unsent row
    carries the rate limit and its retry delay (#192 review)."""
    mocked.add(
        responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=_bridge_locations(60), status=200
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        status=429,
        headers={"Retry-After": "7"},
    )
    rows = catalog.enrich_locations("NWDP", like="Bridge", use_cache=False)
    # cwms-python retries the 429 itself; only the first batch is ever sent.
    assert len(set(_ts_likes(mocked))) == 1
    assert all(r["enrichment_incomplete"] == "upstream_error" for r in rows)
    assert {r["enrichment_error"]["code"] for r in rows} == {"rate_limited"}
    assert rows[-1]["enrichment_error"]["retry_after_ms"] == 7000


def test_enrich_locations_does_not_cache_a_failed_batch(configured, mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=_bridge_locations(3), status=200)
    state = {"fail": True}
    mocked.add_callback(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        callback=_ts_catalog_callback(fail_when=lambda like: state["fail"]),
    )
    first = catalog.enrich_locations("NWDP", like="Bridge")
    assert all(r["enrichment_incomplete"] == "upstream_error" for r in first)
    state["fail"] = False
    second = catalog.enrich_locations("NWDP", like="Bridge")
    assert all(r["parameter_count"] == 1 for r in second)


def test_enrich_locations_caps_batches_and_marks_the_rest(configured, mocked, monkeypatch) -> None:
    monkeypatch.setattr(catalog, "MAX_TS_LIKE_BATCHES", 2)
    mocked.add(
        responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=_bridge_locations(100), status=200
    )
    mocked.add_callback(
        responses.GET, f"{API_ROOT}catalog/TIMESERIES", callback=_ts_catalog_callback()
    )
    rows = catalog.enrich_locations("NWDP", like="Bridge")
    assert len(_ts_likes(mocked)) == 2
    capped = [r for r in rows if r.get("enrichment_incomplete") == "batch_limit"]
    assert capped and "enrichment_error" not in capped[0]
    assert len(capped) + sum(r["parameter_count"] for r in rows) == 100


def test_enrich_locations_without_like_still_raises_on_upstream_failure(configured, mocked) -> None:
    """Unfiltered enrichment feeds ghost counts and sibling repair; a failure
    there must stay an error, never zero-filled rows (#190 review)."""
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=_bridge_locations(3), status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", status=500)
    with pytest.raises(CwmsToolsError):
        catalog.enrich_locations("NWDP", use_cache=False)


def test_get_locations_catalog_wraps_5xx_as_temporary_upstream_error(configured, mocked) -> None:
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
    assert env.temporary is True
    assert env.source.endpoints_called == ["/catalog/LOCATIONS"]


def test_get_locations_catalog_wraps_429_as_rate_limited_with_retry_after(
    configured, mocked
) -> None:
    """SC2: a 429 must surface as `rate_limited` (retryable) with `retry_after_ms`
    parsed from the `Retry-After` header — previously every non-404 4xx was
    misclassified as non-retryable `upstream_error`, so a backing-off agent
    would give up instead of waiting."""
    fixture = json.loads(fixture_path("cda_429_rate_limited.json").read_text())
    resp = fixture["response"]
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        status=resp["status"],
        headers=resp["headers"],
        body=resp["body"],
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_locations_catalog("SWT", use_cache=False)
    env = ex_info.value.envelope
    expected = fixture["expected_envelope"]
    assert env.code is ErrorCode.RATE_LIMITED
    assert env.code.value == expected["code"]
    assert env.temporary is expected["temporary"]
    assert env.retry_after_ms == expected["retry_after_ms"]


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
    assert env.temporary is False


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


def test_malformed_json_200_surfaces_as_upstream_error(configured, mocked) -> None:
    """cwms-python 1.1.2+ raises `ApiError` for an unparseable 200 body instead of
    handing back the raw text (#130). That must land in the structured envelope,
    not crash later on a `str` payload."""
    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        status=200,
        content_type="application/json",
        body="{not json",
    )
    with pytest.raises(CwmsToolsError) as ex_info:
        catalog.get_locations_catalog("SWT", use_cache=False)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.source.endpoints_called == ["/catalog/LOCATIONS"]


def test_upstream_failure_emits_no_warning_or_error_log_records(
    configured, mocked, caplog: pytest.LogCaptureFixture
) -> None:
    """We report upstream failures in the structured envelope, so a failing call
    must not also print an unstructured log line. cwms-python <= 1.0.x wrote
    `logging.error(...)` to the root logger (we filtered it); 1.1.2+ logs at
    DEBUG on `cwms.api` instead, which this pins (#130)."""
    import logging

    mocked.add(
        method=responses.GET,
        url=f"{API_ROOT}catalog/LOCATIONS",
        status=500,
        body="Internal Server Error",
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CwmsToolsError):
        catalog.get_locations_catalog("SWT", use_cache=False)
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert loud == [], [r.getMessage() for r in loud]


def test_location_catalog_cached_flips_after_get_locations_catalog(configured, mocked) -> None:
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        json=SWT_LOCATIONS_PAYLOAD,
        status=200,
    )
    assert catalog.location_catalog_cached("SWT") is False
    catalog.get_locations_catalog("SWT")
    assert catalog.location_catalog_cached("SWT") is True
    assert catalog.location_catalog_cached("SWT", like="FOSS") is False
    assert catalog.location_catalog_cached("MVS") is False


SUGGEST_LOCATIONS = {
    "locations": [
        {
            "office-id": "SWT",
            "name": "FOSS",
            "public-name": "Foss Reservoir",
            "latitude": 35.55,
            "longitude": -98.97,
        },
        {
            "office-id": "SWT",
            "name": "ARBU",
            "public-name": "Arbuckle Lake",
            "latitude": 34.4,
            "longitude": -97.0,
        },
        {
            "office-id": "SWT",
            "name": "UBLW_S1-D21,0ft",
            "public-name": None,
            "latitude": 47.6,
            "longitude": -122.3,
        },
    ]
}


def _arm_suggest(mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=SUGGEST_LOCATIONS, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)


def test_location_catalog_row_is_exact_and_case_sensitive(configured, mocked) -> None:
    _arm_suggest(mocked)
    row = catalog.location_catalog_row("SWT", "FOSS")
    assert row is not None and row["public-name"] == "Foss Reservoir"
    assert catalog.location_catalog_row("SWT", "Foss") is None


@pytest.mark.parametrize(
    ("query", "exact", "fuzzy_contains"),
    [
        ("Foss", "FOSS", []),  # case-insensitive id
        ("foss reservoir", "FOSS", []),  # case-insensitive public name
        ("Foss Lake", None, ["FOSS"]),  # not a ci match; fuzzy by first word
        ("Arbuckle", None, ["ARBU"]),
        ("Zzzz", None, []),
    ],
)
def test_suggest_location(configured, mocked, query, exact, fuzzy_contains) -> None:
    _arm_suggest(mocked)
    got_exact, fuzzy = catalog.suggest_location("SWT", query)
    assert got_exact == exact
    for name in fuzzy_contains:
        assert name in fuzzy


def test_suggest_location_ambiguous_alias_is_not_an_exact_match(configured, mocked) -> None:
    rows = {
        "locations": [
            {
                "office-id": "SWT",
                "name": "A",
                "public-name": "Shared",
                "latitude": 1.0,
                "longitude": 1.0,
            },
            {
                "office-id": "SWT",
                "name": "B",
                "public-name": "Shared",
                "latitude": 2.0,
                "longitude": 2.0,
            },
            {
                "office-id": "SWT",
                "name": "SHARED",
                "public-name": "Other",
                "latitude": 3.0,
                "longitude": 3.0,
            },
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    # The catalog id wins over the alias when both match.
    assert catalog.suggest_location("SWT", "shared") == ("SHARED", [])
    rows["locations"].pop()  # no id match: the alias is ambiguous → candidates only
    mocked.replace(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    assert catalog.suggest_location("SWT", "shared", use_cache=False) == (None, ["A", "B"])


def test_suggest_location_fuzzy_id_label_beats_an_identical_alias(configured, mocked) -> None:
    """T10: OTHER's public name equals DAM1's id; the fuzzy pass must map the
    shared label to the id's own record, not let the alias shadow it."""
    rows = {
        "locations": [
            {"office-id": "SWT", "name": "OTHER", "public-name": "Dam1"},
            {"office-id": "SWT", "name": "DAM1", "public-name": "Something Else"},
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    assert catalog.suggest_location("SWT", "dam1x") == (None, ["DAM1"])


def test_suggest_location_handles_comma_and_regex_names(configured, mocked) -> None:
    """Review focus #1: ids with commas/regex metacharacters flow through the
    matcher untouched (no regex is built from the query here)."""
    _arm_suggest(mocked)
    assert catalog.suggest_location("SWT", "ublw_s1-d21,0ft") == ("UBLW_S1-D21,0ft", [])
    assert catalog.suggest_location("SWT", "(.*)") == (None, [])


def test_location_not_found_error_carries_suggestion_and_intent(configured, mocked) -> None:
    _arm_suggest(mocked)
    err = catalog.location_not_found_error("SWT", "Foss")
    env = err.envelope
    assert env.code is ErrorCode.NOT_FOUND
    assert env.details is not None and env.details.field == "name" and env.details.value == "Foss"
    assert "did you mean 'FOSS'" in env.message
    assert env.repair is not None and env.repair.tool == "cwms_search_places"
    assert env.repair.arguments == {"query": "Foss", "office": "SWT"}
    assert err.repair_intent is not None and err.repair_intent.overrides == {"name": "FOSS"}
    fuzzy_only = catalog.location_not_found_error("SWT", "Foss Lake")
    assert fuzzy_only.repair_intent is None
    assert fuzzy_only.envelope.details is not None
    assert "FOSS" in (fuzzy_only.envelope.details.reason or "")


def test_data_bearing_siblings_filters_by_parameter(configured, mocked) -> None:
    rows = {
        "locations": [
            {"office-id": "NWDP", "name": "GWLW", "latitude": 47.65, "longitude": -122.33},
            {
                "office-id": "NWDP",
                "name": "GWLW_S1-D3,0ft",
                "latitude": 47.6501,
                "longitude": -122.33,
            },
            {
                "office-id": "NWDP",
                "name": "GWLW_S1-D13,0ft",
                "latitude": 47.6502,
                "longitude": -122.33,
            },
        ]
    }
    ts = {
        "entries": [
            {"name": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
            {"name": "GWLW_S1-D13,0ft.Conc-DissolvedOxygen.Inst.1Hour.0.IRIDIUM-REV"},
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts, status=200)
    assert catalog.data_bearing_siblings("NWDP", "GWLW") == ["GWLW_S1-D13,0ft", "GWLW_S1-D3,0ft"]
    assert catalog.data_bearing_siblings("NWDP", "GWLW", parameter="Temp-Water") == [
        "GWLW_S1-D3,0ft"
    ]
    assert catalog.data_bearing_siblings("NWDP", "NOPE") == []


def test_enrich_locations_builds_co_location_in_one_pass(configured, mocked, monkeypatch) -> None:
    """#169: enrichment must not call the pairwise `co_located` per row."""
    from cwms_tools.core import geo

    rows = [
        {"office-id": "SWT", "name": f"S{i}", "latitude": 35.0 + i * 0.0001, "longitude": -98.0}
        for i in range(50)
    ]
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": rows}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    calls = {"pairwise": 0}
    real = geo.co_located

    def counting(*a, **k):
        calls["pairwise"] += 1
        return real(*a, **k)

    monkeypatch.setattr(catalog, "co_located", counting, raising=False)
    monkeypatch.setattr(geo, "co_located", counting)
    enriched = catalog.enrich_locations("SWT")
    assert calls["pairwise"] == 0
    # ~11 m spacing: every row is within 100 m of its 9 nearest neighbours.
    assert "S1" in next(r for r in enriched if r["name"] == "S0")["co_located"]


def test_enrich_locations_row_without_coordinates_is_never_a_sibling(configured, mocked) -> None:
    rows = [
        {"office-id": "SWT", "name": "A1", "latitude": 35.0, "longitude": -98.0},
        {"office-id": "SWT", "name": "A2", "latitude": 35.0, "longitude": -98.0},
        {"office-id": "SWT", "name": "NOLAT", "latitude": None, "longitude": -98.0},
    ]
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": rows}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    by_name = {r["name"]: r for r in catalog.enrich_locations("SWT")}
    assert by_name["NOLAT"]["co_located"] == []
    assert by_name["A1"]["co_located"] == ["A2"]
    assert by_name["A2"]["co_located"] == ["A1"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_to_geopoint_rejects_non_finite_coordinates(bad) -> None:
    base = {"office-id": "SWT", "name": "A", "latitude": 35.0, "longitude": -98.0}
    assert catalog._to_geopoint(base) is not None
    assert catalog._to_geopoint({**base, "latitude": bad}) is None
    assert catalog._to_geopoint({**base, "longitude": bad}) is None
