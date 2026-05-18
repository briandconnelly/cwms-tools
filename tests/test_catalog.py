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
