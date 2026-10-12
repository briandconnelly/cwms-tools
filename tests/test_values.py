"""Tests for the value-tools: get_value (status classification) + get_history."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

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


def test_get_value_raises_not_found_with_repair_when_parameter_absent(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        with pytest.raises(CwmsToolsError) as ex_info:
            values.get_value("SWT", "FOSS", "Flow-In")
    err = ex_info.value.envelope
    assert err.code is ErrorCode.NOT_FOUND
    assert err.details is not None
    assert err.details.field == "parameter"
    assert err.details.value == "Flow-In"
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
# #66: raw-point response cap
# --------------------------------------------------------------------------


def test_get_history_raw_caps_response_points(configured) -> None:
    """`rollup='raw'` (the default) caps `values` at MAX_RAW_HISTORY_POINTS
    even when the upstream fetch returns more, so a long window over a
    high-frequency series can't return unbounded rows. `summary`/
    `value_count` still reflect the FULL fetched window, not just the
    capped `values` (#66)."""
    cap = values.MAX_RAW_HISTORY_POINTS
    total = cap + 500
    start = datetime(2026, 5, 1, tzinfo=UTC)
    pts = [(start + timedelta(minutes=i), float(i)) for i in range(total)]
    payload = _history(
        points=pts,
        begin=start,
        end=start + timedelta(minutes=total),
    )
    assert payload["value_count"] == total
    assert len(payload["values"]) == cap
    assert payload["truncated"] is True
    assert payload["truncation_hint"] is not None
    assert "begin_iso" in payload["truncation_hint"]
    assert payload["summary"]["count"] == total  # not affected by the response cap

    last_returned = payload["values"][-1]["timestamp"]
    assert payload["next_begin"] is not None
    last_dt = datetime.fromisoformat(last_returned.replace("Z", "+00:00"))
    next_dt = datetime.fromisoformat(payload["next_begin"].replace("Z", "+00:00"))
    assert next_dt == last_dt + timedelta(milliseconds=1)  # continuation seam, not window end


def test_get_history_raw_under_cap_is_not_truncated(configured) -> None:
    """A window under the cap is unaffected: no truncation, full `values`."""
    pts = [(datetime(2026, 5, 17, 18, i, tzinfo=UTC), float(i)) for i in range(10)]
    payload = _history(
        points=pts,
        begin=datetime(2026, 5, 17, 18, tzinfo=UTC),
        end=datetime(2026, 5, 17, 19, tzinfo=UTC),
    )
    assert len(payload["values"]) == 10
    assert payload["truncated"] is False
    assert payload["next_begin"] is None
    assert payload["truncation_hint"] is None


def test_get_history_rollup_bucket_mode_not_subject_to_raw_cap(configured) -> None:
    """`rollup='hourly'/'daily'` isn't subject to the raw-point response cap —
    it already returns compact per-bucket aggregates instead of raw points."""
    cap = values.MAX_RAW_HISTORY_POINTS
    total = cap + 200
    start = datetime(2026, 5, 1, tzinfo=UTC)
    pts = [(start + timedelta(minutes=i), float(i)) for i in range(total)]
    payload = _history(
        "daily",
        points=pts,
        begin=start,
        end=start + timedelta(minutes=total),
    )
    assert payload["value_count"] == total
    assert payload["values"] == []
    assert payload["truncated"] is False
    assert len(payload["buckets"]) > 0


def test_get_history_local_cap_does_not_overclaim_when_upstream_also_truncated(
    configured, monkeypatch
) -> None:
    """When the upstream fetch itself was already clipped at its page cap
    (before reaching the requested window end), the local response cap also
    fires (5,000 << 300,000) and takes precedence for `next_begin` — but the
    hint must NOT claim that switching to `rollup='hourly'/'daily'` would
    cover the full requested window, since `summary`/`buckets` are computed
    from that same upstream-clipped fetch, not the full window (#66)."""
    monkeypatch.setattr(
        values.timeseries,
        "require_canonical_ts_id",
        lambda *a, **k: "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
    )
    cap = values.MAX_RAW_HISTORY_POINTS
    total = cap + 500
    start = datetime(2026, 5, 1, tzinfo=UTC)
    fetched_values = [
        {
            "timestamp": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            "value": float(i),
            "quality": 0,
        }
        for i in range(total)
    ]
    fake_series = {
        "unit": "ft",
        "begin": start.isoformat(),
        "end": (start + timedelta(days=365)).isoformat(),
        "values": fetched_values,
        "truncated": True,  # upstream page cap fired before reaching requested end
        "next_begin": "2099-01-01T00:00:00Z",  # upstream's own (less precise) continuation
        "truncation_hint": "hit upstream page cap of 300000; retry with begin_iso=<next_begin>.",
    }
    monkeypatch.setattr(values.timeseries, "fetch_window", lambda *a, **k: fake_series)

    payload = values.get_history(
        "SWT", "FOSS", "Elev", begin=start, end=start + timedelta(days=365)
    )

    assert payload["truncated"] is True
    assert len(payload["values"]) == cap
    assert payload["value_count"] == total
    # The local cap's next_begin (right after the last RETURNED point) takes
    # precedence over the upstream fetch's own (coarser) next_begin.
    assert payload["next_begin"] != "2099-01-01T00:00:00Z"
    hint = payload["truncation_hint"]
    assert hint is not None
    assert "not the full requested window" in hint
    assert "rollup" not in hint  # must not promise a rollup retry recovers full coverage


def test_get_history_raw_cap_sorts_out_of_order_points_before_capping(
    configured, monkeypatch
) -> None:
    """`_cap_raw_points` sorts by timestamp before slicing, so an
    out-of-order upstream response still keeps the genuinely-earliest N
    points and derives `next_begin` from the true last-kept point — a naive
    positional slice could otherwise skip a point permanently (#66)."""
    monkeypatch.setattr(
        values.timeseries,
        "require_canonical_ts_id",
        lambda *a, **k: "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
    )
    cap = values.MAX_RAW_HISTORY_POINTS
    total = cap + 500
    start = datetime(2026, 5, 1, tzinfo=UTC)
    chronological = [
        {
            "timestamp": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            "value": float(i),
        }
        for i in range(total)
    ]
    # Shuffle: move the point that belongs right at the cap boundary to the
    # very front of the list, out of order.
    boundary = chronological.pop(cap - 1)
    shuffled = [boundary, *chronological]
    fake_series = {
        "unit": "ft",
        "begin": start.isoformat(),
        "end": (start + timedelta(minutes=total)).isoformat(),
        "values": shuffled,
        "truncated": False,
        "next_begin": None,
        "truncation_hint": None,
    }
    monkeypatch.setattr(values.timeseries, "fetch_window", lambda *a, **k: fake_series)

    payload = values.get_history(
        "SWT", "FOSS", "Elev", begin=start, end=start + timedelta(minutes=total)
    )

    returned_timestamps = {v["timestamp"] for v in payload["values"]}
    assert boundary["timestamp"] in returned_timestamps  # not skipped despite being out of order
    assert len(payload["values"]) == cap
    # Points are returned chronologically after the defensive sort.
    assert [v["timestamp"] for v in payload["values"]] == sorted(returned_timestamps)


def test_get_history_raw_cap_hint_has_no_next_begin_when_timestamps_unparseable(
    configured, monkeypatch
) -> None:
    """If every capped point lacks a parseable timestamp, `next_begin` is
    `None` — the hint must not reference it and should fall back to
    'narrow the window' instead (#66)."""
    monkeypatch.setattr(
        values.timeseries,
        "require_canonical_ts_id",
        lambda *a, **k: "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
    )
    cap = values.MAX_RAW_HISTORY_POINTS
    total = cap + 500
    start = datetime(2026, 5, 1, tzinfo=UTC)
    fetched_values = [{"timestamp": None, "value": float(i)} for i in range(total)]
    fake_series = {
        "unit": "ft",
        "begin": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "values": fetched_values,
        "truncated": False,
        "next_begin": None,
        "truncation_hint": None,
    }
    monkeypatch.setattr(values.timeseries, "fetch_window", lambda *a, **k: fake_series)

    payload = values.get_history("SWT", "FOSS", "Elev", begin=start, end=start + timedelta(days=1))

    assert payload["truncated"] is True
    assert payload["next_begin"] is None
    assert payload["truncation_hint"] is not None
    assert "begin_iso" not in payload["truncation_hint"]
    assert "narrow the window" in payload["truncation_hint"]


def test_timestamp_sort_key_pushes_malformed_string_timestamps_last() -> None:
    """A timestamp that IS a string but doesn't parse as RFC3339 (e.g. a
    garbage/malformed value) must sort last like a missing timestamp, not by
    its raw string value — otherwise it could sort ahead of genuinely-earlier
    valid points and get kept over them by `_cap_raw_points` (#66)."""
    valid_early = {"timestamp": "2026-05-01T00:00:00Z", "value": 1.0}
    valid_late = {"timestamp": "2026-05-02T00:00:00Z", "value": 2.0}
    malformed = {"timestamp": "not-a-timestamp", "value": 3.0}
    missing = {"timestamp": None, "value": 4.0}

    ordered = sorted([malformed, valid_late, missing, valid_early], key=values._timestamp_sort_key)
    assert ordered[0] is valid_early
    assert ordered[1] is valid_late
    assert {id(ordered[2]), id(ordered[3])} == {id(malformed), id(missing)}


def test_timestamp_sort_key_orders_chronologically_not_lexicographically() -> None:
    """Mixed fractional-second precision must sort by real time, not raw string
    bytes. For `"...:00Z"` vs `"...:00.500Z"`, `'.'` (0x2E) < `'Z'` (0x5A), so a
    lexicographic key ranks the `.500` point *earlier* though it is 500 ms
    *later* — capping on the wrong boundary and deriving a bogus `next_begin`.
    Sorting by parsed epoch fixes it (post-0.5.0 review)."""
    whole = {"timestamp": "2026-05-01T00:00:00Z", "value": 1.0}
    frac = {"timestamp": "2026-05-01T00:00:00.500Z", "value": 2.0}
    # Feed them in lexicographic ('.' < 'Z') order to prove the key re-sorts.
    ordered = sorted([frac, whole], key=values._timestamp_sort_key)
    assert ordered == [whole, frac]
    # `next_begin` must derive from the true-latest point (frac), + 1 ms.
    assert values._next_begin_from_points(ordered) == "2026-05-01T00:00:00.501000Z"


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
    # The parent exists in the catalog (an empty profile under an absent parent
    # is not_found, #163); it just has no depth children publishing the parameter.
    monkeypatch.setattr(
        catalog_mod,
        "get_locations_catalog",
        lambda office, use_cache=True: {"entries": [{"office": "NWDP", "name": "GWLW_S1"}]},
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

    with pytest.raises(CwmsToolsError) as exc:
        values.get_profile("NWDP", "GWLW_S1", "Temp-Water", window=timedelta(hours=hours))
    assert exc.value.envelope.code == ErrorCode.USAGE_ERROR
    details = exc.value.envelope.details
    assert details is not None
    assert details.field == "window_hours"


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


def test_get_value_normalizes_office_case_and_rejects_unknown_office(
    configured, validating_directory
) -> None:
    directory = [{"office-id": "SWT", "long-name": "Tulsa District", "type": "DIS"}]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=directory, status=200)
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
        payload = values.get_value("swt", "FOSS", "Elev")
        assert payload["office_id"] == "SWT"
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("XYZ", "FOSS", "Elev")
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD
    # No catalog call was made for the unknown office (cwms-python sends `office=`).
    assert not [c for c in mocked.calls if "office=XYZ" in (c.request.url or "")]


def test_get_history_rejects_unknown_office(configured, validating_directory) -> None:
    directory = [{"office-id": "SWT", "long-name": "Tulsa District", "type": "DIS"}]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=directory, status=200)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_history(
                "XYZ",
                "FOSS",
                "Elev",
                begin=datetime(2026, 5, 17, tzinfo=UTC),
                end=datetime(2026, 5, 18, tzinfo=UTC),
            )
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD


def _intent(err: CwmsToolsError):
    assert err.repair_intent is not None
    return err.repair_intent


def _details(err: CwmsToolsError):
    assert err.envelope.details is not None
    return err.envelope.details


def _repair(err: CwmsToolsError):
    assert err.envelope.repair is not None
    return err.envelope.repair


GWLW_LOCATIONS = {
    "locations": [
        {"office-id": "NWDP", "name": "GWLW_S1", "latitude": 47.65, "longitude": -122.33},
        {"office-id": "NWDP", "name": "GWLW_S1-D3,0ft", "latitude": 47.6501, "longitude": -122.33},
        {"office-id": "NWDP", "name": "GWLW_S1-D13,0ft", "latitude": 47.6502, "longitude": -122.33},
        # A neighbouring string whose children are co-located with GWLW_S1 but are
        # NOT its children: a ghost GWLW_S2 must not repair to a GWLW_S1-D… child.
        {"office-id": "NWDP", "name": "GWLW_S2", "latitude": 47.6503, "longitude": -122.33},
        {"office-id": "NWDP", "name": "LONELY", "latitude": 46.0, "longitude": -120.0},
    ]
}
GWLW_TS = {
    "entries": [
        {"name": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
        {"name": "GWLW_S1-D13,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
    ]
}


def _arm_catalog(mocked, *, locations, ts_for_location, ts_full) -> None:
    # `ts_ids_for_location` scopes its request with a `like` query key; the
    # office-wide enrichment fetch has no `like` (it does carry
    # `timeseries-group-like`, so the regex anchors on `[?&]like=` — a bare
    # `like=` would match both). Register the scoped one first.
    mocked.add(
        responses.GET,
        re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
        json=ts_for_location,
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts_full, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locations, status=200)


def test_case_a_not_found_lists_published_parameters_and_suggests_close_match(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json={
                "entries": [
                    {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"},
                    {"name": "FOSS.Stor.Inst.1Day.0.Ccp-Rev"},
                ]
            },
            status=200,
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("SWT", "FOSS", "Stro")
    err = ex.value
    assert err.envelope.code is ErrorCode.NOT_FOUND
    assert "Elev, Stor" in (_details(err).reason or "")
    assert err.envelope.repair is not None and _repair(err).tool == "cwms_list_parameters"
    assert err.repair_intent is not None
    assert _intent(err).overrides == {"parameter": "Stor"}


def test_case_a_wrong_case_parameter_is_an_exact_suggestion(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("SWT", "FOSS", "elev")
    assert _intent(ex.value).overrides == {"parameter": "Elev"}


def test_case_b_depth_string_ghost_points_at_shallowest_child_and_names_profile(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "GWLW_S1", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.GHOST_LOCATION
    assert _details(err).field == "name"
    assert "GWLW_S1-D3,0ft" in (_details(err).reason or "")
    assert _intent(err).next_step == "read_data_bearing_sibling"
    assert _intent(err).overrides == {"name": "GWLW_S1-D3,0ft"}
    assert "cwms_get_profile" in (_intent(err).alternative or "")
    assert _repair(err).tool == "cwms_search_places"


def test_case_b_neighbouring_string_children_are_not_this_strings_children(configured) -> None:
    """Codex #7: a nearby depth-tagged sibling is not evidence that get_profile on
    THIS name works. GWLW_S2 is a ghost next to GWLW_S1's children: list them,
    but no same-tool intent (two flat candidates) and no profile alternative."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "GWLW_S2", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.GHOST_LOCATION
    assert err.repair_intent is None
    assert _repair(err).tool == "cwms_search_places"
    assert "GWLW_S1-D3,0ft" in (_details(err).reason or "")


def test_case_b_single_flat_sibling_is_the_intent(configured) -> None:
    locations = {
        "locations": [
            {
                "office-id": "NWDM",
                "name": "GARR-Garrison_Dam",
                "latitude": 47.5,
                "longitude": -101.4,
            },
            {"office-id": "NWDM", "name": "GARR", "latitude": 47.5001, "longitude": -101.4},
        ]
    }
    ts_full = {"entries": [{"name": "GARR.Elev.Inst.1Hour.0.Best-MRBWM"}]}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=locations, ts_for_location={"entries": []}, ts_full=ts_full)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDM", "GARR-Garrison_Dam", "Elev")
    assert ex.value.envelope.code is ErrorCode.GHOST_LOCATION
    assert _intent(ex.value).overrides == {"name": "GARR"}


def test_case_c_barren_everywhere_is_ghost_with_search_repair(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked,
            locations=GWLW_LOCATIONS,
            ts_for_location={"entries": []},
            ts_full={"entries": []},
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "LONELY", "Elev")
    err = ex.value
    assert err.envelope.code is ErrorCode.GHOST_LOCATION
    assert err.repair_intent is None
    assert _repair(err).tool == "cwms_search_places"
    assert _repair(err).arguments == {"query": "LONELY", "office": "NWDP"}


def test_missing_name_is_not_found_not_ghost(configured) -> None:
    """#163: a misspelled/wrong-case name must never be promoted to ghost_location."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "gwlw_s1", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.NOT_FOUND
    assert _details(err).field == "name"
    assert _intent(err).overrides == {"name": "GWLW_S1"}


def test_get_history_inherits_ghost_location(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_history(
                "NWDP",
                "GWLW_S1",
                "Temp-Water",
                begin=datetime(2026, 5, 17, tzinfo=UTC),
                end=datetime(2026, 5, 18, tzinfo=UTC),
            )
    assert ex.value.envelope.code is ErrorCode.GHOST_LOCATION


# --------------------------------------------------------------------------
# One enrichment per ghost path (#96): call counts
# --------------------------------------------------------------------------


def _endpoint(url: str) -> str:
    """`TIMESERIES(scoped)` vs `TIMESERIES(office-wide)`, `catalog/LOCATIONS`, `offices`."""
    path = url.split(API_ROOT, 1)[1].split("?", 1)[0]
    if path == "catalog/TIMESERIES":
        scoped = "like=" in url.replace("group-like=", "")
        return "TIMESERIES(scoped)" if scoped else "TIMESERIES(office-wide)"
    return path


def test_case_c_makes_four_upstream_calls_with_cache(configured, validating_directory) -> None:
    """offices + scoped TIMESERIES + LOCATIONS + office-wide TIMESERIES: the
    two sibling filters share one enrichment, and the existence check reuses
    the same locations catalog."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=[{"office-id": "NWDP", "type": "DIV"}])
        _arm_catalog(
            mocked,
            locations=GWLW_LOCATIONS,
            ts_for_location={"entries": []},
            ts_full={"entries": []},
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "LONELY", "Elev")
        hits = [_endpoint(str(c.request.url)) for c in mocked.calls]
    assert ex.value.envelope.code is ErrorCode.GHOST_LOCATION
    assert sorted(hits) == sorted(
        ["offices", "TIMESERIES(scoped)", "catalog/LOCATIONS", "TIMESERIES(office-wide)"]
    )


@pytest.mark.parametrize(
    ("name", "code", "expected"),
    [
        (
            "GWLW_S1",  # case B
            ErrorCode.GHOST_LOCATION,
            ["TIMESERIES(scoped)", "catalog/LOCATIONS", "TIMESERIES(office-wide)"],
        ),
        (
            "LONELY",  # case C
            ErrorCode.GHOST_LOCATION,
            ["TIMESERIES(scoped)", "catalog/LOCATIONS", "TIMESERIES(office-wide)"],
        ),
        (
            "gwlw_s1",  # wrong case: not_found, no enrichment at all
            ErrorCode.NOT_FOUND,
            ["TIMESERIES(scoped)", "catalog/LOCATIONS"],
        ),
    ],
    ids=["case-b", "case-c", "not-found"],
)
def test_ghost_paths_fetch_each_endpoint_once_without_cache(
    configured, name, code, expected
) -> None:
    from cwms_tools.core import timeseries

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS
        )
        with pytest.raises(CwmsToolsError) as ex:
            timeseries.require_canonical_ts_id("NWDP", name, "Temp-Water", use_cache=False)
        hits = [_endpoint(str(c.request.url)) for c in mocked.calls]
    assert ex.value.envelope.code is code
    assert hits == expected


def test_ghost_and_not_found_errors_name_the_endpoints_hit(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(
            mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS
        )
        with pytest.raises(CwmsToolsError) as ghost:
            values.get_value("NWDP", "GWLW_S1", "Temp-Water")
        with pytest.raises(CwmsToolsError) as missing:
            values.get_value("NWDP", "gwlw_s1", "Temp-Water")
    trail = ["/catalog/TIMESERIES", "/catalog/LOCATIONS"]
    assert ghost.value.envelope.source.endpoints_called == trail
    assert missing.value.envelope.source.endpoints_called == trail


# --------------------------------------------------------------------------
# get_profile: an absent parent is not_found, not an empty success (#163)
# --------------------------------------------------------------------------


def test_get_profile_wrong_case_parent_is_not_found(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=GWLW_LOCATIONS, ts_for_location=GWLW_TS, ts_full=GWLW_TS)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_profile("NWDP", "gwlw_s1", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.NOT_FOUND
    assert _details(err).field == "name"
    assert _intent(err).next_step == "retry_with_catalog_id"
    assert _intent(err).overrides == {"name": "GWLW_S1"}
    assert err.envelope.source.endpoints_called == ["/catalog/LOCATIONS"]


def test_get_profile_catalog_failure_keeps_empty_success(configured, monkeypatch) -> None:
    """A failed existence read is not evidence of absence: profile keeps its
    empty, noted success rather than turning a transient failure into not_found."""
    from cwms_tools.core import catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod, "enrich_locations", lambda office, like=None, use_cache=True: []
    )

    def _down(office, use_cache=True):
        raise CwmsToolsError.of(ErrorCode.UPSTREAM_ERROR, "down", temporary=True)

    monkeypatch.setattr(catalog_mod, "get_locations_catalog", _down)
    payload = values.get_profile("NWDP", "gwlw_s1", "Temp-Water")
    assert payload["sensor_count"] == 0
    assert "note" in payload


def _levels_listing(*rows) -> dict:
    return {"levels": list(rows)}


def _level(lid: str, specified: str, date: str) -> dict:
    return {
        "location-level-id": lid,
        "specified-level-id": specified,
        "office-id": "SWT",
        "level-date": date,
    }


def test_get_value_dedupes_level_revisions_and_classifies_nominal_inside_conservation(
    configured,
) -> None:
    """#162 end to end: two revisions per id, floor levels crossed -> one row per
    id and `nominal`, with the value fetched at the newest revision."""
    listing = _levels_listing(
        _level("FOSS.Elev.Inst.0.Top of Flood", "Top of Flood", "1900-01-01T06:00:00Z"),
        _level("FOSS.Elev.Inst.0.Top of Flood", "Top of Flood", "1961-02-13T06:00:00Z"),
        _level(
            "FOSS.Elev.Inst.0.Top of Conservation", "Top of Conservation", "1961-02-13T06:00:00Z"
        ),
        _level("FOSS.Elev.Inst.0.Top of Inactive", "Top of Inactive", "1961-02-13T06:00:00Z"),
    )
    values_by_id = {
        "Top of Flood": 1662.2,
        "Top of Conservation": 1642.0,
        "Top of Inactive": 1597.2,
    }

    level_urls: list[str] = []

    def level_value(request):
        level_urls.append(request.url.replace("%20", " ").replace("%3A", ":"))
        name = next(k for k in values_by_id if k in request.url.replace("%20", " "))
        body = json.dumps({"constant-value": values_by_id[name], "level-units-id": "ft"})
        return (200, {"Content-Type": "application/json"}, body)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(
                ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                points=[(datetime(2026, 10, 12, 14, tzinfo=UTC), 1637.6)],
            ),
            status=200,
        )
        mocked.add(responses.GET, f"{API_ROOT}levels", json=listing, status=200)
        mocked.add_callback(
            responses.GET, re.compile(rf"{API_ROOT}levels/.*"), callback=level_value
        )
        payload = values.get_value("SWT", "FOSS", "Elev", classify_against_levels=True)
        level_reads = [c for c in mocked.calls if re.search(r"/levels/.+", c.request.url or "")]
    assert payload["status_class"] == "nominal"
    assert payload["level_lookup_status"] == "computed"
    ids = [t["level_id"] for t in payload["thresholds_active"]]
    assert len(ids) == len(set(ids)) == 3
    assert len(level_reads) == 3, "one value read per level id, at the newest revision"
    flood_read = next(u for u in level_urls if "Top of Flood" in u)
    assert "effective-date=1961-02-13" in flood_read
    inactive = next(
        t for t in payload["thresholds_active"] if t["specified_level_id"] == "Top of Inactive"
    )
    assert inactive["zone"] == "nominal"


def test_get_value_with_no_levels_is_unknown_and_unavailable(configured) -> None:
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
    assert payload["status_class"] == "unknown"
    assert payload["level_lookup_status"] == "unavailable"
    assert payload["thresholds_active"] == []


def _details(err: CwmsToolsError):
    assert err.envelope.details is not None
    return err.envelope.details


def test_parse_rfc3339_rejects_naive_and_garbage() -> None:
    with pytest.raises(CwmsToolsError) as ex:
        values.parse_rfc3339("2026-10-05", field="begin_iso")
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD
    assert _details(ex.value).field == "begin_iso"
    assert "T00:00:00Z" in (_details(ex.value).reason or "")
    with pytest.raises(CwmsToolsError):
        values.parse_rfc3339("not-a-date", field="end_iso")
    assert values.parse_rfc3339("2026-10-05T00:00:00Z", field="x") == datetime(
        2026, 10, 5, tzinfo=UTC
    )


@pytest.mark.parametrize("value", ["2026-10-05", "2026-10-05T00:00:00"])
def test_parse_rfc3339_hints_are_followable(value: str) -> None:
    with pytest.raises(CwmsToolsError) as ex:
        values.parse_rfc3339(value, field="x")
    match = re.search(r"e\.g\. (\S+?)\.?$", _details(ex.value).reason or "")
    assert match
    assert values.parse_rfc3339(match.group(1), field="x") == datetime(2026, 10, 5, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-10-05t00:00:00z", "2026-10-05T00:00:00z"])
def test_parse_rfc3339_accepts_lowercase_t_and_z(value: str) -> None:
    assert values.parse_rfc3339(value, field="x") == datetime(2026, 10, 5, tzinfo=UTC)


def test_history_rejects_inverted_window_before_any_upstream_call(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        with pytest.raises(CwmsToolsError) as ex:
            values.get_history(
                "SWT",
                "FOSS",
                "Elev",
                begin=datetime(2026, 5, 18, tzinfo=UTC),
                end=datetime(2026, 5, 17, tzinfo=UTC),
            )
        assert len(mocked.calls) == 0
    assert ex.value.envelope.code is ErrorCode.USAGE_ERROR
    assert _details(ex.value).field == "end"
    assert "2026-05-18" in (_details(ex.value).reason or "")
    assert "2026-05-17" in (_details(ex.value).reason or "")


def test_history_rejects_equal_instants_across_offsets() -> None:
    """+05:30 and Z spellings of the same instant are an empty window."""
    with pytest.raises(CwmsToolsError) as ex:
        values.require_window(
            datetime.fromisoformat("2026-05-17T05:30:00+05:30"),
            datetime.fromisoformat("2026-05-17T00:00:00+00:00"),
            begin_field="begin_iso",
            end_field="end_iso",
        )
    assert ex.value.envelope.code is ErrorCode.USAGE_ERROR
    assert _details(ex.value).field == "end_iso"


def test_require_window_rejects_naive_datetime() -> None:
    with pytest.raises(CwmsToolsError) as ex:
        values.require_window(datetime(2026, 5, 17), datetime(2026, 5, 18, tzinfo=UTC))
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD
    assert _details(ex.value).field == "begin"
