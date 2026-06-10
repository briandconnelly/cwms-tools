"""Tests for the `cwms-tools offices` office-code discovery command."""

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

_LIVE_SHAPE = [
    {"name": "NWO", "long-name": "Omaha District", "type": "DIS", "reports-to": "NWDM"},
    {"name": "NWDM", "long-name": "Missouri River Region", "type": "MSCR", "reports-to": "NWD"},
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


def test_offices_emits_directory_and_guidance(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        result = runner.invoke(app, ["--machine", "offices"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["partial"] is False
    names = {o["name"] for o in payload["offices"]}
    assert names == {"NWO", "NWDM"}
    assert payload["guidance"]["nw_rollup_targets"]["NWO"] == "NWDM"


def test_offices_degrades_to_partial_without_erroring(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        result = runner.invoke(app, ["--machine", "offices"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["partial"] is True
    assert payload["count"] > 0


def test_schema_lists_offices_command() -> None:
    result = runner.invoke(app, ["--machine", "schema"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    cmd = {c["path"]: c for c in doc["commands"]}.get("cwms-tools offices")
    assert cmd is not None
    assert cmd["latency_class"] == "network"
