"""Tests for the `cwms-tools place ...` and `cwms-tools region browse` commands."""

from __future__ import annotations

import json

import cwms
import pytest
import responses
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.cli.commands import place as place_cmd
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
    # New since round 2: cap metadata is always present on the response.
    assert payload["truncated"] is False
    assert payload["total_count"] == len(payload["results"])
    assert payload["limit"] == 50


def test_place_search_respects_limit_flag(configured) -> None:
    """`--limit N` caps the response at N rows. Beyond the cap, results
    are dropped (data-bearing rows sort first so the useful ones stay).
    `truncated` stays False — the cap is pageable via `next_cursor`, so
    `has_more` is the signal to page, not `truncated` (#73)."""
    locations_payload = {
        "locations": [
            {
                "office-id": "SWT",
                "name": f"Site_{i:04d}",
                "latitude": 35.0,
                "longitude": -98.0,
            }
            for i in range(10)
        ]
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
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
        result = runner.invoke(app, ["place", "search", "Site", "--office", "SWT", "--limit", "3"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["truncated"] is False
    assert payload["has_more"] is True
    assert payload["total_count"] == 10
    assert len(payload["results"]) == 3


def test_place_search_parameter_filter_drops_non_publishers(configured) -> None:
    """`--parameter` filters out data-bearing rows that don't publish it.
    The new behavior addresses Codex review F2 (Fremont Bridge probe)."""
    locations_payload = {
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
                "latitude": 47.65,
                "longitude": -122.35,
            },
        ]
    }
    ts_payload = {
        "entries": [
            {"name": "FBLW.Volt-Battery.Inst.1Hour.0.IRIDIUM-REV"},
            {"name": "FBLW_D1-D5,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
        ]
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        for _ in range(4):
            mocked.add(
                responses.GET,
                f"{API_ROOT}catalog/LOCATIONS",
                json=locations_payload,
                status=200,
            )
            mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts_payload, status=200)
        result = runner.invoke(
            app,
            [
                "place",
                "search",
                "Fremont Bridge",
                "--office",
                "NWDP",
                "--parameter",
                "Temp-Water",
            ],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    names = [r["name"] for r in payload["results"]]
    assert "FBLW_D1-D5,0ft" in names
    assert "FBLW" not in names
    assert payload["parameter"] == "Temp-Water"
    assert payload["nearby_non_matching_count"] >= 1


def test_place_search_repeatable_office_flag(configured) -> None:
    """Multiple `--office` flags fan out across each. The response's
    `offices_searched` reflects what actually ran."""
    nwdp_locs = {"locations": []}
    nwdp_ts = {"entries": []}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        for _ in range(4):
            mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=nwdp_locs, status=200)
            mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=nwdp_ts, status=200)
        result = runner.invoke(app, ["place", "search", "anything", "-o", "NWDP", "-o", "NWDM"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["offices_searched"] == ["NWDP", "NWDM"]


def test_place_search_returns_ghost_office_error_for_nwo() -> None:
    """#69: `place search` has a real `--office` flag — repair retries the
    SAME command (never a different one), echoing query/parameter/limit/
    detail with office rolled up into the swap target."""
    result = runner.invoke(
        app,
        ["place", "search", "Bear Creek", "--office", "NWO", "--parameter", "Elev", "-n", "10"],
    )
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ghost_office"
    assert payload["error"]["details"]["field"] == "office"
    repair = payload["error"]["repair"]
    # Repair names the runnable CLI command, not the MCP tool (#69) — the
    # sibling describe/parameters/value commands do the same.
    assert repair["tool"] == "cwms-tools place search"
    assert repair["arguments"] == {
        "query": "Bear Creek",
        "parameter": "Elev",
        "limit": 10,
        "detail": "summary",
        "office": "NWDM",
    }


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
    assert result.stdout == ""  # stdout stays success-only
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"


def test_place_describe_ghost_office_names_spec_not_office() -> None:
    """#68 review: `place describe` has no `--office` flag — the retryable
    arg for a ghost_office error is the combined `spec`, not the nonexistent
    "office" (the default MCP-facing translation for this producer-internal
    `office_id` field)."""
    result = runner.invoke(app, ["place", "describe", "NWO/FTPK"])
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ghost_office"
    assert payload["error"]["details"]["field"] == "spec"
    # #69: repair retries `place describe` with the SAME name, office
    # rolled up into a fresh `OFFICE/NAME` spec — not a different command.
    # #69 review: `tool` names the actual CLI invocation, not the MCP tool
    # (the MCP tool takes separate office/name, not a combined `spec`).
    repair = payload["error"]["repair"]
    assert repair["tool"] == "cwms-tools place describe"
    assert repair["arguments"] == {"spec": "NWDM/FTPK", "detail": "summary"}


def test_place_parameters_ghost_office_names_spec_not_office() -> None:
    result = runner.invoke(app, ["place", "parameters", "NWO/FTPK"])
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ghost_office"
    assert payload["error"]["details"]["field"] == "spec"
    repair = payload["error"]["repair"]
    assert repair["tool"] == "cwms-tools place parameters"
    assert repair["arguments"] == {"spec": "NWDM/FTPK"}


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
    assert result.stdout == ""
    payload = json.loads(result.stderr)
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
    result = runner.invoke(
        app, ["region", "browse", "--office", "NWO", "--state", "MT", "--limit", "10"]
    )
    assert result.exit_code == 12  # GHOST exit
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ghost_office"
    # #68: `--office` is the flag; `office_id` (the producer-internal name
    # `core.locations`/`core.catalog` raise with) is never a real CLI flag.
    assert payload["error"]["details"]["field"] == "office"
    # #69: repair retries the SAME command (never switches), office swapped
    # to the rollup target, echoing the rest of the original call.
    repair = payload["error"]["repair"]
    assert repair["tool"] == "cwms-tools region browse"
    assert repair["arguments"] == {"office": "NWDM", "state": "MT", "limit": 10}


def test_region_browse_ghost_office_repair_echoes_bbox() -> None:
    result = runner.invoke(
        app,
        [
            "region",
            "browse",
            "--office",
            "NWO",
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
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stderr)
    repair = payload["error"]["repair"]
    assert repair["arguments"] == {
        "south": 35.0,
        "west": -100.0,
        "north": 36.0,
        "east": -98.0,
        "limit": 50,
        "office": "NWDM",
    }


def test_region_browse_rejects_partial_bbox_naming_first_missing_corner() -> None:
    """#68: `field` must name a real `--<flag>`, not the synthetic "bbox"."""
    result = runner.invoke(
        app, ["region", "browse", "--office", "SWT", "--south", "30.0", "--north", "40.0"]
    )
    assert result.exit_code == 2  # USAGE_ERROR
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "usage_error"
    assert payload["error"]["details"]["field"] == "west"


def test_place_search_accepts_cursor(monkeypatch):
    captured = {}

    def fake_search(query, *, office=None, parameter=None, limit=None, cursor=None, use_cache=True):
        captured["cursor"] = cursor
        return {
            "query": query,
            "results": [],
            "has_more": False,
            "next_cursor": None,
            "total_count": 0,
            "source": {"fingerprint": "x"},
        }

    monkeypatch.setattr(place_cmd.places, "search_places", fake_search)
    result = runner.invoke(app, ["place", "search", "L", "-o", "NWDM", "--cursor", "TOKEN"])
    assert result.exit_code == 0
    assert captured["cursor"] == "TOKEN"
