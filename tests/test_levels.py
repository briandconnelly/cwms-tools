"""Tests for the levels module (list_levels, resolve_applicable_level, classify)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import cwms
import pytest
import responses

from cwms_tools.core import levels, session
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


def test_list_levels_returns_entries(configured) -> None:
    payload = {
        "levels": [
            {
                "location-level-id": "FOSS.Elev.Inst.0.Flood Stage",
                "specified-level-id": "Flood Stage",
                "office-id": "SWT",
                "level-date": "2026-01-01T00:00:00Z",
            }
        ]
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}levels", json=payload, status=200)
        rows = levels.list_levels("SWT", level_id_mask="FOSS.Elev.*")
    assert len(rows) == 1
    assert rows[0]["specified-level-id"] == "Flood Stage"


def test_list_levels_builds_mask_from_location_and_parameter(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)
        rows = levels.list_levels("SWT", location="FOSS", parameter="Elev")
        # Inspect inside the `with` — `responses` clears `.calls` on exit.
        assert rows == []
        call_url = str(mocked.calls[0].request.url)
        assert "FOSS.Elev." in call_url


def test_list_levels_wraps_upstream_failure_as_upstream_error(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}levels", status=500)
        with pytest.raises(CwmsToolsError) as ex:
            levels.list_levels("SWT", level_id_mask="*")
    assert ex.value.envelope.code is ErrorCode.UPSTREAM_ERROR


def test_resolve_applicable_level_picks_latest_levelDate(configured) -> None:
    payload = {
        "levels": [
            {
                "location-level-id": "FOSS.Elev.Inst.0.Flood Stage",
                "specified-level-id": "Flood Stage",
                "level-date": "2024-01-01T00:00:00Z",
            },
            {
                "location-level-id": "FOSS.Elev.Inst.0.Flood Stage",
                "specified-level-id": "Flood Stage",
                "level-date": "2026-01-01T00:00:00Z",
            },
        ]
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}levels", json=payload, status=200)
        row = levels.resolve_applicable_level(
            "SWT",
            location="FOSS",
            parameter="Elev",
            at=datetime(2026, 5, 17, tzinfo=UTC),
        )
    assert row is not None
    assert row["level-date"] == "2026-01-01T00:00:00Z"


def test_resolve_applicable_level_honors_expirationDate(configured) -> None:
    payload = {
        "levels": [
            {
                "location-level-id": "FOSS.Elev.Inst.0.Flood Stage",
                "specified-level-id": "Flood Stage",
                "level-date": "2026-01-01T00:00:00Z",
                "expiration-date": "2026-04-01T00:00:00Z",
            }
        ]
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}levels", json=payload, status=200)
        row = levels.resolve_applicable_level(
            "SWT",
            location="FOSS",
            parameter="Elev",
            at=datetime(2026, 5, 17, tzinfo=UTC),
        )
    # Expiration was before our `at`, so no applicable row.
    assert row is None


def test_fetch_level_value_constant_short_circuits(configured) -> None:
    config = {
        "location-level-id": "FOSS.Elev.Inst.0.Spillway Crest",
        "office-id": "SWT",
        "constant-value": 1675.0,
        "level-units-id": "ft",
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, re.compile(rf"{API_ROOT}levels/.*"), json=config, status=200)
        result = levels.fetch_level_value(
            "FOSS.Elev.Inst.0.Spillway Crest",
            office="SWT",
            effective_date=datetime(2026, 5, 17, tzinfo=UTC),
        )
    assert result["variety"] == "constant"
    assert result["value"] == 1675.0
    assert result["source_workaround"] is None


def test_classify_returns_unknown_for_no_observation() -> None:
    status, _ = levels.classify(None, [])
    assert status == "unknown"


def test_classify_below_all_thresholds_is_nominal() -> None:
    thresholds = [
        {"specified_level_id": "Flood Stage", "value": 1700.0, "unit": "ft"},
        {"specified_level_id": "Action Stage", "value": 1680.0, "unit": "ft"},
    ]
    status, annotated = levels.classify(1650.0, thresholds)
    assert status == "nominal"
    # Each annotated threshold has the relation field.
    assert all(t["relation"] == "below" for t in annotated)


def test_classify_above_action_stage_is_action() -> None:
    thresholds = [
        {"specified_level_id": "Action Stage", "value": 1680.0, "unit": "ft"},
    ]
    status, _ = levels.classify(1690.0, thresholds)
    assert status == "action"


AT = datetime(2026, 10, 12, 14, tzinfo=UTC)


def _row(lid: str, date: str, *, exp: str | None = None, specified: str = "X") -> dict:
    row = {"location-level-id": lid, "level-date": date, "specified-level-id": specified}
    if exp is not None:
        row["expiration-date"] = exp
    return row


def test_applicable_revisions_keeps_one_row_per_level_id() -> None:
    """#162: live SWT/FOSS returns 24 rows for 13 ids — a 1900-01-01 and a
    1961-02-13 revision of most levels — which doubled `thresholds_active`."""
    rows = [
        _row("FOSS.Elev.Inst.0.Top of Flood", "1900-01-01T06:00:00Z"),
        _row("FOSS.Elev.Inst.0.Top of Flood", "1961-02-13T06:00:00Z"),
        _row("FOSS.Elev.Inst.0.Streambed", "1961-02-13T06:00:00Z"),
        _row("FOSS.Elev.Inst.0.Future", "2030-01-01T00:00:00Z"),
        _row("FOSS.Elev.Inst.0.Expired", "2000-01-01T00:00:00Z", exp="2001-01-01T00:00:00Z"),
        _row("FOSS.Elev.Inst.0.Undated", ""),
    ]
    kept = levels.applicable_revisions(rows, at=AT)
    assert [(levels.level_id_of(r), r["level-date"]) for r in kept] == [
        ("FOSS.Elev.Inst.0.Top of Flood", "1961-02-13T06:00:00Z"),
        ("FOSS.Elev.Inst.0.Streambed", "1961-02-13T06:00:00Z"),
    ]


def test_applicable_revisions_equal_dates_keep_listing_order() -> None:
    a = _row("L", "2000-01-01T00:00:00Z", specified="first")
    b = _row("L", "2000-01-01T00:00:00Z", specified="second")
    assert levels.applicable_revisions([a, b], at=AT)[0]["specified-level-id"] == "first"
