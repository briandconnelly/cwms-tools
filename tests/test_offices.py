"""Tests for office discovery (`core.offices`) backing the `cwms://offices` resource."""

from __future__ import annotations

import cwms
import pytest
import responses

from cwms_tools.core import offices, session
from cwms_tools.core.cache import Cache, set_cache

API_ROOT = "https://example.test/cwms-data/"

_LIVE_SHAPE = [
    {"name": "NWO", "long-name": "Omaha District", "type": "DIS", "reports-to": "NWDM"},
    {"name": "NWDM", "long-name": "Missouri River Region", "type": "MSCR", "reports-to": "NWD"},
    {"name": "HQ", "long-name": "Headquarters", "type": "HQ", "reports-to": "HQ"},
]


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", API_ROOT)
    session._state["config"] = None
    cwms.init_session(api_root=API_ROOT, pool_connections=4)
    session.configure_session()
    cache = Cache(directory=tmp_path / "cache")
    set_cache(cache)
    yield
    cache.close()
    set_cache(None)
    session._state["config"] = None


def test_list_offices_parses_and_normalizes(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        records, used_fallback = offices.list_offices()

    assert used_fallback is False
    # Sorted by name.
    assert [r["name"] for r in records] == ["HQ", "NWDM", "NWO"]
    nwo = next(r for r in records if r["name"] == "NWO")
    assert nwo["long_name"] == "Omaha District"
    assert nwo["type"] == "DIS"
    assert nwo["type_label"] == "district"
    assert nwo["reports_to"] == "NWDM"


def test_list_offices_caches_after_first_fetch(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        first, _ = offices.list_offices()
    # Second call must not hit upstream (no mock active → would raise).
    second, used_fallback = offices.list_offices()
    assert second == first
    assert used_fallback is False


def test_list_offices_falls_back_when_upstream_fails(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        records, used_fallback = offices.list_offices()

    assert used_fallback is True
    names = {r["name"] for r in records}
    assert {"NWDM", "NWDP", "SWT"} <= names
    # Fallback records are name-only (no upstream metadata).
    assert all(set(r) == {"name"} for r in records)


def test_list_offices_falls_back_on_empty_payload(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=[], status=200)
        _, used_fallback = offices.list_offices()
    assert used_fallback is True


def test_unknown_type_code_passes_through_as_label(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}offices",
            json=[{"name": "ZZZ", "type": "NEWCODE"}],
            status=200,
        )
        records, _ = offices.list_offices()
    assert records[0]["type"] == "NEWCODE"
    assert records[0]["type_label"] == "NEWCODE"


def test_list_office_ids_derives_from_records(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        ids, used_fallback = offices.list_office_ids()
    assert ids == ["HQ", "NWDM", "NWO"]
    assert used_fallback is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Nested under a wrapper key.
        ({"offices": [{"name": "SWT"}]}, ["SWT"]),
        ({"entries": [{"name": "MVS"}]}, ["MVS"]),
        # Bare string items.
        (["SWT", "MVS"], ["SWT", "MVS"]),
        # Alternate id keys.
        ([{"office-id": "NWDM"}], ["NWDM"]),
        # Junk items are skipped; nameless dicts dropped.
        ([1, None, {"type": "DIS"}, {"name": "SWL"}], ["SWL"]),
        # Unrecognized top-level shape.
        ("nope", []),
    ],
)
def test_parse_office_records_tolerates_shapes(raw, expected) -> None:
    records = offices._parse_office_records(raw)
    assert [r["name"] for r in records] == expected


def test_cached_offices_for_locations(configured) -> None:
    from cwms_tools.core.cache import build_cache_key, get_cache

    cache = get_cache()
    cfg = session.current_config()
    cache.set(build_cache_key("location_catalog", "SWT", "", api_root=cfg.api_root), {}, ttl=None)
    assert offices.cached_offices_for_locations() == ["SWT"]


def test_offices_payload_shape_and_guidance(configured) -> None:
    from cwms_tools.mcp.resources import offices_payload

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        payload = offices_payload()

    assert payload["count"] == 3
    assert payload["partial"] is False
    assert [o["name"] for o in payload["offices"]] == ["HQ", "NWDM", "NWO"]
    guidance = payload["guidance"]
    assert guidance["nw_district_stubs"] == ["NWK", "NWO", "NWP", "NWS", "NWW"]
    assert guidance["nw_rollup_targets"]["NWO"] == "NWDM"
    assert guidance["nw_rollup_targets"]["NWP"] == "NWDP"
    assert "NWDM" in guidance["nw_regional_rollup"]


def test_offices_payload_marks_partial_on_degraded_fallback(configured) -> None:
    from cwms_tools.mcp.resources import offices_payload

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        payload = offices_payload()

    assert payload["partial"] is True
    assert payload["count"] > 0
