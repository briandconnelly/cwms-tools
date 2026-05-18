"""Tests for the `cwms-tools place ...` and `cwms-tools region browse` commands."""

from __future__ import annotations

import json

import cwms
import pytest
import responses
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import session
from cwms_tools.core.cache import Cache, set_cache

API_ROOT = "https://example.test/cwms-data/"

runner = CliRunner()


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


LOCATIONS = {
    "locations": [
        {
            "office-id": "SWT",
            "name": "FOSS",
            "location-kind": "PROJECT",
            "latitude": 35.55,
            "longitude": -98.97,
            "state-initial": "OK",
            "public-name": "Foss Reservoir",
        },
    ],
}
TIMESERIES = {
    "entries": [
        {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", "last-update": "2026-05-17T18:00:00Z"},
    ],
}
LOCATION_SINGLE = {
    "office-id": "SWT",
    "name": "FOSS",
    "location-kind": "PROJECT",
    "latitude": 35.55,
    "longitude": -98.97,
    "horizontal-datum": "NAD83",
    "state-initial": "OK",
    "timezone-name": "America/Chicago",
}
PROJECT = {"location": LOCATION_SINGLE, "authorizing-law": "FCA-1944"}


def _arm(mocked):
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", json=LOCATION_SINGLE, status=200)
    mocked.add(responses.GET, f"{API_ROOT}projects/FOSS", json=PROJECT, status=200)


def test_place_search_emits_machine_json(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = runner.invoke(app, ["place", "search", "FOSS", "--office", "SWT"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    assert payload["query"] == "FOSS"
    assert payload["results"][0]["name"] == "FOSS"


def test_place_describe_emits_summary_by_default(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = runner.invoke(app, ["place", "describe", "SWT/FOSS"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["office_id"] == "SWT"
    # Summary mode strips the heavy Location DTO to a triage subset.
    loc = payload["location"]
    assert set(loc.keys()) <= {
        "office-id",
        "name",
        "location-kind",
        "latitude",
        "longitude",
        "public-name",
        "long-name",
        "horizontal-datum",
        "state-initial",
        "nearest-city",
        "timezone-name",
    }


def test_place_describe_emits_full_location_when_detail_full(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = runner.invoke(app, ["place", "describe", "SWT/FOSS", "--detail", "full"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    # Full mode keeps all DTO fields, including the extras like timezone-name.
    assert "timezone-name" in payload["location"]


def test_place_describe_rejects_bad_spec_shape() -> None:
    result = runner.invoke(app, ["place", "describe", "no-slash"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"


def test_place_parameters_lists_grouped_by_publisher(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = runner.invoke(app, ["place", "parameters", "SWT/FOSS"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ts_count"] == 1
    assert payload["by_publisher"][0]["publisher"] == "Ccp-Rev"


def test_region_browse_office_only(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = runner.invoke(app, ["region", "browse", "--office", "SWT"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["office"] == "SWT"
    assert payload["result_count"] == 1
    assert payload["ghost_count"] == 0


def test_region_browse_rejects_partial_bbox() -> None:
    result = runner.invoke(
        app,
        ["region", "browse", "--office", "SWT", "--south", "30.0", "--north", "40.0"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"


def test_region_browse_bbox(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = runner.invoke(
            app,
            [
                "region",
                "browse",
                "--office",
                "SWT",
                "--south",
                "35.0",
                "--west",
                "-100.0",
                "--north",
                "36.0",
                "--east",
                "-98.0",
            ],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["result_count"] == 1
    assert payload["bbox"] == {"south": 35.0, "west": -100.0, "north": 36.0, "east": -98.0}


def test_region_browse_returns_ghost_office_error_for_nwo() -> None:
    result = runner.invoke(app, ["region", "browse", "--office", "NWO"])
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "ghost_office"
    assert payload["error"]["repair"]["tool"] == "cwms_browse_region"
    assert payload["error"]["repair"]["args"]["office"] == "NWDM"
