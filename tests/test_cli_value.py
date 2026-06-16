"""Tests for `cwms-tools value get | history`."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

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


TS_CATALOG = {"entries": [{"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"}]}


def _ts_payload(*, ts_id: str, ts: datetime, value: float) -> dict:
    ms = int(ts.timestamp() * 1000)
    return {
        "name": ts_id,
        "units": "ft",
        "value-columns": [
            {"name": "date-time"},
            {"name": "value"},
            {"name": "quality-code"},
        ],
        "values": [[ms, value, 0]],
    }


def _arm_value(mocked, *, value: float, ts: datetime):
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{API_ROOT}timeseries.*"),
        json=_ts_payload(ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", ts=ts, value=value),
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)


def test_value_get_single_id(configured) -> None:
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["partial"] is False
    assert payload["summary"]["ok"] == 1
    item = payload["results"][0]
    assert item["ok"] is True
    assert item["data"]["value"] == 1648.21
    # Default is value-only; status classification is skipped.
    assert item["data"]["status_class"] == "unknown"
    assert item["data"]["level_lookup_status"] == "skipped"


def test_value_get_with_status_runs_classification(configured) -> None:
    """`--with-status` opts into the slow threshold lookup. Hitting the
    /levels endpoint is required; the response reports the lookup ran to
    completion via level_lookup_status."""
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev", "--with-status"])
        levels_calls = [c for c in mocked.calls if "/levels" in (c.request.url or "")]
        assert levels_calls, "--with-status must hit the levels endpoint"
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    item = payload["results"][0]
    assert item["data"]["level_lookup_status"] in {"computed", "unavailable"}


def test_value_get_partial_failure_exits_nonzero(configured) -> None:
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        # Use a non-existent parameter for the second id so it 404s.
        result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev", "SWT/FOSS/NoSuchParam"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["partial"] is True
    assert payload["summary"]["failed"] == 1
    failed = next(r for r in payload["results"] if not r["ok"])
    assert failed["error"]["code"] == "not_found"
    assert failed["error"]["repair"]["tool"] == "cwms_list_parameters"


def test_value_get_rejects_bad_id_shape() -> None:
    result = runner.invoke(app, ["value", "get", "missing-slashes"])
    assert result.exit_code == 2
    assert result.stdout == ""  # stdout stays success-only
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "usage_error"


def test_value_history_returns_windowed_series(configured) -> None:
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = runner.invoke(
            app,
            [
                "value",
                "history",
                "SWT/FOSS/Elev",
                "--begin",
                "2026-05-17T17:00:00Z",
                "--end",
                "2026-05-17T19:00:00Z",
            ],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ts_id"] == "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"
    assert payload["value_count"] == 1
    # Summary mode strips the `quality` column.
    assert "quality" not in payload["values"][0]


def test_value_history_rejects_bad_datetimes() -> None:
    result = runner.invoke(
        app,
        [
            "value",
            "history",
            "SWT/FOSS/Elev",
            "--begin",
            "not-a-date",
            "--end",
            "still-not",
        ],
    )
    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_field"
    assert payload["error"]["field"] == "begin"  # precise field, not lumped "begin/end"


def test_value_get_rejects_unknown_unit() -> None:
    """`--unit` is a closed set ('EN' or 'SI'). Typer rejects unknown values
    before the command body runs (Codex review F5)."""
    result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev", "--unit", "bogus"])
    assert result.exit_code == 2
    # Typer's choice-validation error names the rejected value.
    combined = (result.stdout or "") + (result.stderr or "")
    assert "bogus" in combined


def test_value_history_rejects_unknown_unit() -> None:
    """Same enum constraint on `value history`."""
    result = runner.invoke(
        app,
        [
            "value",
            "history",
            "SWT/FOSS/Elev",
            "--begin",
            "2026-05-17T17:00:00Z",
            "--end",
            "2026-05-17T19:00:00Z",
            "--unit",
            "bogus",
        ],
    )
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "bogus" in combined


def test_usage_error_writes_full_envelope_to_stderr() -> None:
    """C1/C3: whole-command usage errors emit the FULL ErrorEnvelope (with
    request_id, hint, field) to stderr — not the old hand-built partial dict on
    stdout."""
    result = runner.invoke(app, ["value", "get", "no-slashes"])
    assert result.exit_code == 2
    assert result.stdout == ""
    err = json.loads(result.stderr)["error"]
    assert err["code"] == "usage_error"
    assert err["field"] == "id"
    assert err["request_id"]  # full envelope, not the old partial shape
    assert err["hint"]


def test_value_get_partial_failure_keeps_aggregate_on_stdout(configured) -> None:
    """C1: the bulk `value get` aggregate IS the success payload and stays on
    stdout even on partial failure; only the non-zero exit signals it. NWO is an
    NW-stub office, so resolving it fails with ghost_office without a live call."""
    result = runner.invoke(app, ["value", "get", "NWO/FTPK/Elev"])
    assert result.exit_code == 12  # ghost exit, non-zero
    payload = json.loads(result.stdout)
    assert payload["partial"] is True
    assert payload["summary"]["failed"] == 1
    assert payload["results"][0]["ok"] is False
    assert payload["results"][0]["error"]["code"] == "ghost_office"


def test_value_profile_emits_sorted_profile(configured, monkeypatch) -> None:
    """#26/#27: `value profile` reads a whole depth string in one call, sorted
    shallow→deep with structured depth, and (summary) drops per-sensor ts_id."""
    from cwms_tools.core import values

    canned = {
        "office_id": "NWDP",
        "name": "GWLW_S1",
        "parameter": "Temp-Water",
        "unit": "EN",
        "sensor_count": 2,
        "profile": [
            {
                "name": "GWLW_S1-D3,0ft",
                "depth": {"value": 3.0, "unit": "ft"},
                "value": 67.1,
                "unit": "EN",
                "timestamp": "2026-05-17T18:00:00Z",
                "publisher": "IRIDIUM-REV",
                "ts_id": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV",
            },
            {
                "name": "GWLW_S1-D36,0ft",
                "depth": {"value": 36.0, "unit": "ft"},
                "value": 55.0,
                "unit": "EN",
                "timestamp": "2026-05-17T18:00:00Z",
                "publisher": "IRIDIUM-REV",
                "ts_id": "GWLW_S1-D36,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV",
            },
        ],
    }
    monkeypatch.setattr(values, "get_profile", lambda *a, **k: dict(canned))
    result = runner.invoke(app, ["value", "profile", "NWDP/GWLW_S1/Temp-Water"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sensor_count"] == 2
    assert [s["depth"]["value"] for s in payload["profile"]] == [3.0, 36.0]
    # summary mode drops the chatty per-sensor ts_id
    assert all("ts_id" not in s for s in payload["profile"])


def test_value_profile_rejects_bad_id_shape() -> None:
    result = runner.invoke(app, ["value", "profile", "missing-slashes"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage_error"
