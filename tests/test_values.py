"""Tests for the value-tools: get_value (status classification) + get_history."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import cwms
import pytest
import responses

from cwms_tools.core import session, values
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode

API_ROOT = "https://example.test/cwms-data/"


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


TS_CATALOG = {
    "entries": [
        {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"},
        {"name": "FOSS.Elev.Inst.15Minutes.0.Raw-A2W"},
    ],
}


def _ts_url() -> str:
    return f"{API_ROOT}timeseries"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ts_payload(*, ts_id: str, points: list[tuple[datetime, float]]) -> dict:
    return {
        "name": ts_id,
        "office-id": ts_id.split(".", maxsplit=1)[0],  # unused in our parser
        "units": "ft",
        "value-columns": [
            {"name": "date-time"},
            {"name": "value"},
            {"name": "quality-code"},
        ],
        "values": [[_ms(ts), val, 0] for ts, val in points],
    }


# --------------------------------------------------------------------------
# get_value
# --------------------------------------------------------------------------


def test_get_value_returns_latest_value_without_classification_by_default(
    configured,
) -> None:
    """Default behavior is value-only: no /levels call, status_class=unknown,
    level_lookup_status=skipped. The classification path is reliably slow
    so opting-in via with_status=true is the agent-friendly default."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(
                ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                points=[(datetime(2026, 5, 17, 18, tzinfo=UTC), 1648.21)],
            ),
            status=200,
        )
        # No /levels call should be made on the default path.
        payload = values.get_value("SWT", "FOSS", "Elev")
        levels_calls = [c for c in mocked.calls if "/levels" in (c.request.url or "")]
        assert not levels_calls

    assert payload["ts_id"] == "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"
    assert payload["publisher"] == "Ccp-Rev"
    assert payload["value"] == 1648.21
    assert payload["timestamp"] == "2026-05-17T18:00:00Z"
    assert payload["status_class"] == "unknown"
    assert payload["level_lookup_status"] == "skipped"
    assert payload["thresholds_active"] == []


def test_get_value_classifies_nominal_when_with_status_enabled(configured) -> None:
    """When classify_against_levels=True and no thresholds match, the
    observation is `nominal` and level_lookup_status reflects completion."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(
                ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                points=[(datetime(2026, 5, 17, 18, tzinfo=UTC), 1648.21)],
            ),
            status=200,
        )
        mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)
        payload = values.get_value("SWT", "FOSS", "Elev", classify_against_levels=True)

    assert payload["status_class"] == "nominal"
    assert payload["thresholds_active"] == []


def test_get_value_raises_not_found_with_repair_when_parameter_absent(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        with pytest.raises(CwmsToolsError) as ex_info:
            values.get_value("SWT", "FOSS", "Flow-In")
    err = ex_info.value.envelope
    assert err.code is ErrorCode.NOT_FOUND
    assert err.field == "parameter"
    assert err.offending_value == "Flow-In"
    assert err.repair is not None
    assert err.repair.tool == "cwms_list_parameters"


def test_get_value_classifies_flood_when_above_flood_stage(configured) -> None:
    """Observation above a `Flood Stage` threshold should classify as `flood`."""
    levels_payload = {
        "levels": [
            {
                "location-level-id": "FOSS.Elev.Inst.0.Flood Stage",
                "specified-level-id": "Flood Stage",
                "office-id": "SWT",
                "level-date": "2026-01-01T00:00:00Z",
            }
        ]
    }
    level_value_payload = {
        "constant-value": 1640.0,
        "level-units-id": "ft",
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(
                ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                points=[(datetime(2026, 5, 17, 18, tzinfo=UTC), 1650.0)],
            ),
            status=200,
        )
        mocked.add(responses.GET, f"{API_ROOT}levels", json=levels_payload, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{API_ROOT}levels/.*"),
            json=level_value_payload,
            status=200,
        )
        payload = values.get_value("SWT", "FOSS", "Elev", classify_against_levels=True)

    assert payload["value"] == 1650.0
    assert payload["status_class"] == "flood"
    active = payload["thresholds_active"]
    assert len(active) == 1
    assert active[0]["specified_level_id"] == "Flood Stage"
    assert active[0]["relation"] == "above"
    assert active[0]["delta"] == pytest.approx(10.0)


# --------------------------------------------------------------------------
# get_history
# --------------------------------------------------------------------------


def test_get_history_returns_windowed_values(configured) -> None:
    pts = [
        (datetime(2026, 5, 17, 18, 0, tzinfo=UTC), 1648.0),
        (datetime(2026, 5, 17, 18, 15, tzinfo=UTC), 1648.2),
        (datetime(2026, 5, 17, 18, 30, tzinfo=UTC), 1648.5),
    ]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", points=pts),
            status=200,
        )
        payload = values.get_history(
            "SWT",
            "FOSS",
            "Elev",
            begin=datetime(2026, 5, 17, 17, tzinfo=UTC),
            end=datetime(2026, 5, 17, 19, tzinfo=UTC),
        )

    assert payload["ts_id"] == "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"
    assert payload["value_count"] == 3
    assert payload["values"][0]["timestamp"].startswith("2026-05-17T18:00")
    assert payload["values"][-1]["value"] == pytest.approx(1648.5)
    assert payload["truncated"] is False


# --------------------------------------------------------------------------
# next_begin continuation timestamp
# --------------------------------------------------------------------------


def test_fetch_window_emits_next_begin_when_truncated(monkeypatch):
    from cwms_tools.core import timeseries

    cap = timeseries._UPSTREAM_PAGE_SIZE_CAP
    last_ms = 1_700_000_000_000
    values_data = [[last_ms - (cap - 1 - i) * 60000, float(i), 0] for i in range(cap)]
    payload = {"values": values_data, "units": "ft"}

    class _Resp:
        json = payload

    monkeypatch.setattr(timeseries.ts_api, "get_timeseries", lambda **kw: _Resp())
    out = timeseries.fetch_window(
        "t",
        office="NWDM",
        begin=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2099, 1, 1, tzinfo=UTC),  # far future forces truncation
        unit="EN",
    )
    assert out["truncated"] is True
    expected = datetime.fromtimestamp((last_ms + 1) / 1000, tz=UTC)
    assert out["next_begin"] == expected.isoformat().replace("+00:00", "Z")


def test_fetch_window_next_begin_none_when_not_truncated(monkeypatch):
    from cwms_tools.core import timeseries

    payload = {"values": [[1_700_000_000_000, 1.0, 0]], "units": "ft"}

    class _Resp:
        json = payload

    monkeypatch.setattr(timeseries.ts_api, "get_timeseries", lambda **kw: _Resp())
    out = timeseries.fetch_window(
        "t",
        office="NWDM",
        begin=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
        unit="EN",
    )
    assert out["truncated"] is False
    assert out["next_begin"] is None
