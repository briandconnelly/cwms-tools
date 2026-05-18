"""Tests for `cwms-tools value get | history`."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

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
    ts = datetime(2026, 5, 17, 18, tzinfo=timezone.utc)
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


def test_value_get_partial_failure_exits_nonzero(configured) -> None:
    ts = datetime(2026, 5, 17, 18, tzinfo=timezone.utc)
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
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"


def test_value_history_returns_windowed_series(configured) -> None:
    ts = datetime(2026, 5, 17, 18, tzinfo=timezone.utc)
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
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "invalid_field"
