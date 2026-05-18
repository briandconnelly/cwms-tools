"""Tests that exercise the MCP tool handlers end-to-end.

The handlers are thin async adapters over `core/*` — these tests drive
them through `server.call_tool` with mocked CDA traffic so the per-tool
`_shape_detail` logic and structured-error path are covered.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import cwms
import pytest
import responses

from cwms_tools.core import session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.mcp.server import build_server

API_ROOT = "https://example.test/cwms-data/"


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", API_ROOT)
    monkeypatch.delenv("_CWMS_TOOLS_NO_CACHE", raising=False)
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
            "public-name": "Foss Reservoir",
            "location-kind": "PROJECT",
            "latitude": 35.55,
            "longitude": -98.97,
            "state-initial": "OK",
        }
    ]
}

TIMESERIES = {
    "entries": [
        {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", "last-update": "2026-05-17T18:00:00Z"},
    ]
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

PROJECT_PAYLOAD = {"location": LOCATION_SINGLE, "authorizing-law": "FCA-1944"}


def _arm(mocked):
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", json=LOCATION_SINGLE, status=200)
    mocked.add(responses.GET, f"{API_ROOT}projects/FOSS", json=PROJECT_PAYLOAD, status=200)


def _ts_payload(*, value: float, ts: datetime) -> dict:
    return {
        "name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        "units": "ft",
        "value-columns": [
            {"name": "date-time"},
            {"name": "value"},
            {"name": "quality-code"},
        ],
        "values": [[int(ts.timestamp() * 1000), value, 0]],
    }


def _arm_value(mocked, *, value: float, ts: datetime):
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{API_ROOT}timeseries.*"),
        json=_ts_payload(value=value, ts=ts),
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, arguments=args))


def _branch(structured: dict | None) -> dict:
    """Tolerate FastMCP's `{result: {...}}` wrapper for union return types."""
    if structured is None:
        return {}
    return structured.get("result", structured)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def test_search_places_handler(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = _call(server, "cwms_search_places", {"query": "FOSS", "office": "SWT"})
    payload = _branch(result.structured_content)
    assert payload["results"][0]["name"] == "FOSS"
    # M9 envelope: every successful task response must carry source.fingerprint.
    assert "source" in payload
    assert "fingerprint" in payload["source"]
    assert len(payload["source"]["fingerprint"]) == 64


def test_describe_place_handler_strips_in_summary(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = _call(server, "cwms_describe_place", {"office": "SWT", "name": "FOSS"})
    payload = _branch(result.structured_content)
    # Summary mode keeps only the triage subset of the Location DTO.
    assert set(payload["location"].keys()) <= {
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


def test_list_parameters_handler(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = _call(server, "cwms_list_parameters", {"office": "SWT", "name": "FOSS"})
    payload = _branch(result.structured_content)
    assert payload["ts_count"] == 1
    assert payload["by_publisher"][0]["publisher"] == "Ccp-Rev"


def test_browse_region_handler_rejects_partial_bbox(configured) -> None:
    server = build_server()
    result = _call(
        server,
        "cwms_browse_region",
        {"office": "SWT", "south": 30.0, "north": 40.0},
    )
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"


def test_browse_region_handler_returns_ghost_office_for_nwo(configured) -> None:
    server = build_server()
    result = _call(server, "cwms_browse_region", {"office": "NWO"})
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ghost_office"


def test_get_value_handler(configured) -> None:
    server = build_server()
    ts = datetime(2026, 5, 17, 18, tzinfo=timezone.utc)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = _call(
            server,
            "cwms_get_value",
            {"office": "SWT", "location": "FOSS", "parameter": "Elev"},
        )
    payload = _branch(result.structured_content)
    assert payload["value"] == 1648.21
    assert payload["publisher"] == "Ccp-Rev"
    # Summary mode strips chatty fields from per-threshold rows.
    assert all(
        "level_id" not in t and "source_workaround" not in t
        for t in payload.get("thresholds_active", [])
    )


def test_get_history_handler_rejects_bad_datetimes(configured) -> None:
    server = build_server()
    result = _call(
        server,
        "cwms_get_history",
        {
            "office": "SWT",
            "location": "FOSS",
            "parameter": "Elev",
            "begin_iso": "not-a-date",
            "end_iso": "still-not",
        },
    )
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_field"


def test_get_history_handler_returns_values(configured) -> None:
    server = build_server()
    ts = datetime(2026, 5, 17, 18, tzinfo=timezone.utc)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = _call(
            server,
            "cwms_get_history",
            {
                "office": "SWT",
                "location": "FOSS",
                "parameter": "Elev",
                "begin_iso": "2026-05-17T17:00:00Z",
                "end_iso": "2026-05-17T19:00:00Z",
            },
        )
    payload = _branch(result.structured_content)
    assert payload["value_count"] == 1
    # In summary mode quality codes are surfaced as null; the field stays in
    # the schema so a single parser handles both detail levels.
    assert payload["values"][0]["quality"] is None


def test_publishers_for_parameter_handler(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
        result = _call(
            server,
            "cwms_publishers_for_parameter",
            {"parameter": "Elev", "offices": ["SWT"]},
        )
    payload = _branch(result.structured_content)
    assert any(p["publisher"] == "Ccp-Rev" for p in payload["publishers"])
    assert payload["coverage"]["complete"] is True
