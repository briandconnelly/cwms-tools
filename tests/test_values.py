"""Tests for the value-tools: get_value (status classification) + get_history."""

from __future__ import annotations

import re
from collections.abc import Sequence
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


def _ts_payload(*, ts_id: str, points: Sequence[tuple[datetime, float | None]]) -> dict:
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
    # rollup defaults to raw: points are returned, no buckets.
    assert payload["rollup"] == "raw"
    assert "buckets" not in payload


# --------------------------------------------------------------------------
# #25: server-side summary + rollup
# --------------------------------------------------------------------------


def _history(rollup="raw", *, points, begin, end):
    """Drive `values.get_history` with a mocked window of `points`."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", points=points),
            status=200,
        )
        return values.get_history("SWT", "FOSS", "Elev", begin=begin, end=end, rollup=rollup)


def test_get_history_always_includes_summary(configured) -> None:
    """The summary block lets agents answer 'how has X changed' without pulling
    and hand-reducing every point (a token cost and a correctness risk)."""
    pts = [
        (datetime(2026, 5, 17, 18, 0, tzinfo=UTC), 10.0),
        (datetime(2026, 5, 17, 18, 15, tzinfo=UTC), 14.0),
        (datetime(2026, 5, 17, 18, 30, tzinfo=UTC), 12.0),
    ]
    s = _history(
        points=pts,
        begin=datetime(2026, 5, 17, 17, tzinfo=UTC),
        end=datetime(2026, 5, 17, 19, tzinfo=UTC),
    )["summary"]
    assert s["count"] == 3
    assert s["first"] == pytest.approx(10.0)
    assert s["last"] == pytest.approx(12.0)
    assert s["min"] == pytest.approx(10.0)
    assert s["max"] == pytest.approx(14.0)
    assert s["mean"] == pytest.approx(12.0)
    assert s["delta"] == pytest.approx(2.0)  # last - first


def test_summarize_derives_first_last_by_timestamp_not_order() -> None:
    """first/last are by timestamp, so an out-of-order window still yields the
    correct earliest/latest values and delta (not list-position based)."""
    s = values._summarize(
        [
            {"timestamp": "2026-05-17T18:30:00Z", "value": 12.0},  # latest
            {"timestamp": "2026-05-17T18:00:00Z", "value": 10.0},  # earliest
            {"timestamp": "2026-05-17T18:15:00Z", "value": 14.0},
        ]
    )
    assert s is not None
    assert s["first"] == pytest.approx(10.0)  # earliest by timestamp
    assert s["last"] == pytest.approx(12.0)  # latest by timestamp
    assert s["delta"] == pytest.approx(2.0)
    assert s["min"] == pytest.approx(10.0)
    assert s["max"] == pytest.approx(14.0)


def test_summarize_ignores_values_without_timestamp() -> None:
    """A numeric value with no timestamp can't be ordered, so it's excluded from
    the summary entirely (it must not become a spurious first/last)."""
    s = values._summarize(
        [
            {"timestamp": None, "value": 999.0},  # no timestamp → excluded
            {"timestamp": "2026-05-17T18:00:00Z", "value": 10.0},
            {"timestamp": "2026-05-17T18:30:00Z", "value": 12.0},
        ]
    )
    assert s is not None
    assert s["count"] == 2
    assert s["first"] == pytest.approx(10.0)
    assert s["last"] == pytest.approx(12.0)
    assert s["max"] == pytest.approx(12.0)  # 999.0 excluded


def test_get_history_summary_is_null_key_when_window_empty(configured) -> None:
    """No numeric observations → `summary` is null, but the KEY is still present
    (both in the core dict and after model serialization via _keep_null) so
    callers can rely on `summary` existing."""
    from cwms_tools.core.models import HistoryResponse, SourceMeta

    payload = _history(
        points=[],
        begin=datetime(2026, 5, 17, 17, tzinfo=UTC),
        end=datetime(2026, 5, 17, 19, tzinfo=UTC),
    )
    assert "summary" in payload
    assert payload["summary"] is None
    assert payload["value_count"] == 0
    # The compact serializer keeps the null `summary` key (it is in _keep_null).
    dumped = HistoryResponse.model_validate(
        {**payload, "source": SourceMeta(fingerprint="x" * 64).model_dump(mode="json")}
    ).model_dump(mode="json")
    assert "summary" in dumped
    assert dumped["summary"] is None


def test_get_history_summary_ignores_nulls(configured) -> None:
    """Null observations (gaps) don't poison min/mean/first/last."""
    pts = [
        (datetime(2026, 5, 17, 18, 0, tzinfo=UTC), None),
        (datetime(2026, 5, 17, 18, 15, tzinfo=UTC), 20.0),
        (datetime(2026, 5, 17, 18, 30, tzinfo=UTC), 30.0),
    ]
    s = _history(
        points=pts,
        begin=datetime(2026, 5, 17, 17, tzinfo=UTC),
        end=datetime(2026, 5, 17, 19, tzinfo=UTC),
    )["summary"]
    assert s["count"] == 2
    assert s["first"] == pytest.approx(20.0)
    assert s["min"] == pytest.approx(20.0)


def test_get_history_hourly_rollup_buckets(configured) -> None:
    """rollup='hourly' replaces raw points with per-hour min/max/mean/count
    buckets — ~Nx fewer rows for a trend question."""
    pts = [
        (datetime(2026, 5, 17, 18, 0, tzinfo=UTC), 10.0),
        (datetime(2026, 5, 17, 18, 30, tzinfo=UTC), 20.0),
        (datetime(2026, 5, 17, 19, 0, tzinfo=UTC), 30.0),
        (datetime(2026, 5, 17, 19, 30, tzinfo=UTC), 40.0),
    ]
    payload = _history(
        "hourly",
        points=pts,
        begin=datetime(2026, 5, 17, 17, tzinfo=UTC),
        end=datetime(2026, 5, 17, 20, tzinfo=UTC),
    )
    assert payload["rollup"] == "hourly"
    assert payload["values"] == []
    buckets = payload["buckets"]
    assert len(buckets) == 2
    assert buckets[0]["timestamp"].startswith("2026-05-17T18:00")
    assert buckets[0]["min"] == pytest.approx(10.0)
    assert buckets[0]["max"] == pytest.approx(20.0)
    assert buckets[0]["mean"] == pytest.approx(15.0)
    assert buckets[0]["count"] == 2
    assert buckets[1]["timestamp"].startswith("2026-05-17T19:00")
    assert buckets[1]["mean"] == pytest.approx(35.0)


def test_get_history_daily_rollup_buckets(configured) -> None:
    pts = [
        (datetime(2026, 5, 17, 6, tzinfo=UTC), 1.0),
        (datetime(2026, 5, 17, 18, tzinfo=UTC), 3.0),
        (datetime(2026, 5, 18, 6, tzinfo=UTC), 5.0),
    ]
    payload = _history(
        "daily",
        points=pts,
        begin=datetime(2026, 5, 17, tzinfo=UTC),
        end=datetime(2026, 5, 19, tzinfo=UTC),
    )
    assert payload["rollup"] == "daily"
    buckets = payload["buckets"]
    assert [b["timestamp"][:10] for b in buckets] == ["2026-05-17", "2026-05-18"]
    assert buckets[0]["mean"] == pytest.approx(2.0)
    assert buckets[0]["count"] == 2


def test_get_history_rejects_unknown_rollup(configured) -> None:
    with pytest.raises(CwmsToolsError) as exc:
        _history(
            "weekly",
            points=[],
            begin=datetime(2026, 5, 17, tzinfo=UTC),
            end=datetime(2026, 5, 19, tzinfo=UTC),
        )
    assert exc.value.envelope.code == ErrorCode.USAGE_ERROR


# --------------------------------------------------------------------------
# #26/#27: get_profile (whole-string depth read)
# --------------------------------------------------------------------------


def _profile_catalog():
    return [
        {"name": "GWLW_S1-D36,0ft", "parameters": ["Temp-Water"]},
        {"name": "GWLW_S1-D3,0ft", "parameters": ["Temp-Water"]},
        {"name": "GWLW_S1-D13,0ft", "parameters": ["Temp-Water"]},
        {"name": "GWLW_S1", "parameters": []},  # parent string: no temp itself
        {"name": "GWLW_S1-D25,0ft", "parameters": ["Elev"]},  # wrong parameter -> excluded
        {"name": "OTHER-D5,0ft", "parameters": ["Temp-Water"]},  # different string -> excluded
    ]


def test_get_profile_sorts_shallow_to_deep_with_depth(configured, monkeypatch) -> None:
    from cwms_tools.core import catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "enrich_locations",
        lambda office, like=None, use_cache=True: _profile_catalog(),
    )
    vals = {"GWLW_S1-D3,0ft": 67.0, "GWLW_S1-D13,0ft": 61.0, "GWLW_S1-D36,0ft": 55.0}

    def fake_get_value(office, loc, parameter, **_):
        return {
            "value": vals[loc],
            "unit": "degF",  # actual measurement unit, as the value tools return
            "timestamp": "2026-05-17T18:00:00Z",
            "publisher": "IRIDIUM-REV",
            "ts_id": f"{loc}.Temp-Water.Inst.1Hour.0.IRIDIUM-REV",
        }

    monkeypatch.setattr(values, "get_value", fake_get_value)
    payload = values.get_profile("NWDP", "GWLW_S1", "Temp-Water")

    assert payload["sensor_count"] == 3
    assert [e["name"] for e in payload["profile"]] == [
        "GWLW_S1-D3,0ft",
        "GWLW_S1-D13,0ft",
        "GWLW_S1-D36,0ft",
    ]
    assert payload["profile"][0]["depth"] == {"value": pytest.approx(3.0), "unit": "ft"}
    assert payload["profile"][0]["value"] == pytest.approx(67.0)
    assert payload["profile"][-1]["depth"]["value"] == pytest.approx(36.0)
    # Top-level unit mirrors the actual measurement unit from the sensor reads,
    # not the requested EN/SI system (#32 review).
    assert payload["unit"] == "degF"
    assert "note" not in payload


def test_get_profile_empty_returns_note(configured, monkeypatch) -> None:
    from cwms_tools.core import catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod, "enrich_locations", lambda office, like=None, use_cache=True: []
    )
    payload = values.get_profile("NWDP", "GWLW_S1", "Temp-Water")
    assert payload["sensor_count"] == 0
    assert payload["profile"] == []
    assert "note" in payload


def test_get_profile_degrades_failed_sensor(configured, monkeypatch) -> None:
    from cwms_tools.core import catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "enrich_locations",
        lambda office, like=None, use_cache=True: [
            {"name": "GWLW_S1-D3,0ft", "parameters": ["Temp-Water"]},
            {"name": "GWLW_S1-D13,0ft", "parameters": ["Temp-Water"]},
        ],
    )

    def fake_get_value(office, loc, parameter, **_):
        if loc == "GWLW_S1-D13,0ft":
            raise CwmsToolsError.of(ErrorCode.NOT_FOUND, "gone", field="parameter")
        return {"value": 67.0, "unit": "EN", "timestamp": "t", "publisher": "p", "ts_id": "x"}

    monkeypatch.setattr(values, "get_value", fake_get_value)
    payload = values.get_profile("NWDP", "GWLW_S1", "Temp-Water")
    deep = payload["profile"][1]
    assert deep["name"] == "GWLW_S1-D13,0ft"
    assert deep["value"] is None
    assert deep["error"] == "not_found"


@pytest.mark.parametrize("hours", [-1, 0])
def test_get_profile_rejects_non_positive_window(configured, hours) -> None:
    """A non-positive look-back window (negative inverts begin/end; zero is an
    empty window) is rejected up front as a deterministic usage_error, before
    any catalog call — matching the 'must be positive' contract."""
    from datetime import timedelta

    with pytest.raises(CwmsToolsError) as exc:
        values.get_profile("NWDP", "GWLW_S1", "Temp-Water", window=timedelta(hours=hours))
    assert exc.value.envelope.code == ErrorCode.USAGE_ERROR
    assert exc.value.envelope.field == "window_hours"


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
