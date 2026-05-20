"""Tests for the bounded publisher-by-parameter index (§9.8)."""

from __future__ import annotations

import json

import cwms
import pytest
import responses
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import publishers_index, session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode

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


def _ts_catalog(*tsids: str) -> dict:
    return {"entries": [{"name": t} for t in tsids]}


def test_publishers_for_parameter_indexes_requested_offices(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog(
                "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                "FOSS.Flow-Out.Inst.15Minutes.0.Ccp-Rev",
            ),
            status=200,
        )
        payload = publishers_index.publishers_for_parameter("Elev", offices=["SWT"])

    assert payload["parameter"] == "Elev"
    pubs = {p["publisher"] for p in payload["publishers"]}
    assert pubs == {"Ccp-Rev"}
    assert payload["coverage"]["complete"] is True
    assert payload["coverage"]["offices_indexed"] == ["SWT"]
    assert payload["coverage"]["offices_skipped_for_budget"] == []


def test_publishers_for_parameter_returns_empty_when_parameter_not_published(
    configured,
) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        payload = publishers_index.publishers_for_parameter("Flow-In", offices=["SWT"])

    assert payload["publishers"] == []
    assert payload["coverage"]["complete"] is True


def test_publishers_for_parameter_skips_offices_beyond_budget(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the requested list exceeds the per-call budget, excess offices land in skipped."""
    monkeypatch.setattr(publishers_index, "_budget", lambda: 1)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        # Only the first office gets a catalog response — the budget caps at 1.
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        payload = publishers_index.publishers_for_parameter("Elev", offices=["SWT", "MVS", "MVR"])

    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["offices_indexed"] == ["SWT"]
    assert payload["coverage"]["offices_skipped_for_budget"] == ["MVS", "MVR"]
    assert payload["repair"] is not None
    assert payload["repair"]["tool"] == "cwms_publishers_for_parameter"
    assert payload["repair"]["args"]["parameter"] == "Elev"
    assert payload["repair"]["args"]["offices"] == ["MVS", "MVR"]


def test_publishers_for_parameter_distinguishes_budget_skipped_from_error_skipped(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: an office whose catalog fetch errors lands in `offices_error_skipped`,
    NOT lumped into `offices_skipped_for_budget` — so the agent can tell "hit the
    budget, re-run with these" from "these errored"."""
    monkeypatch.setattr(publishers_index, "_budget", lambda: 1)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        # First office errors (500), second succeeds (consumes the budget of 1),
        # third is budget-skipped and never fetched.
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", status=500, body="err")
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        payload = publishers_index.publishers_for_parameter(
            "Elev", offices=["BADOFFICE", "SWT", "MVS"]
        )

    cov = payload["coverage"]
    assert cov["offices_error_skipped"] == ["BADOFFICE"]
    assert cov["offices_indexed"] == ["SWT"]
    assert cov["offices_skipped_for_budget"] == ["MVS"]
    assert cov["complete"] is False


def test_publishers_for_parameter_returns_locations_known_count(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog(
                "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                "ARBU.Elev.Inst.15Minutes.0.Ccp-Rev",
            ),
            status=200,
        )
        payload = publishers_index.publishers_for_parameter("Elev", offices=["SWT"])
    row = next(p for p in payload["publishers"] if p["publisher"] == "Ccp-Rev")
    assert row["locations_known"] == 2


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_publisher_for_parameter_error_is_structured_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: `publisher for-parameter` now wraps core errors like its siblings — a
    propagating CwmsToolsError becomes a structured envelope on stderr with the
    mapped exit code, not an uncaught traceback on exit 1."""

    def _boom(*_a, **_k):
        raise CwmsToolsError.of(
            ErrorCode.UPSTREAM_ERROR,
            "boom",
            endpoints_called=["/catalog/TIMESERIES"],
            retryable=True,
        )

    monkeypatch.setattr(publishers_index, "publishers_for_parameter", _boom)
    result = runner.invoke(app, ["publisher", "for-parameter", "Elev", "--office", "SWT"])
    assert result.exit_code == 9  # upstream_error -> exit 9 (not the unmapped 1)
    assert result.stdout == ""
    err = json.loads(result.stderr)["error"]
    assert err["code"] == "upstream_error"
    assert err["retryable"] is True


def test_cli_publisher_for_parameter_with_explicit_office(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        result = runner.invoke(app, ["publisher", "for-parameter", "Elev", "--office", "SWT"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["parameter"] == "Elev"
    assert payload["coverage"]["offices_indexed"] == ["SWT"]


def test_cli_publisher_for_parameter_repeats_office_flag(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        result = runner.invoke(
            app,
            [
                "publisher",
                "for-parameter",
                "Elev",
                "--office",
                "SWT",
                "--office",
                "MVS",
            ],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    # Both offices appear in offices_requested, but only the first cache hit
    # was registered; the test just confirms the flag was accepted.
    assert {"SWT", "MVS"} <= set(payload["coverage"]["offices_requested"])
