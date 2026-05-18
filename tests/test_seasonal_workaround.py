"""Test the seasonal-level workaround branch (cwms-python issue #286).

When `get_location_level` returns a configuration with `seasonalValues` /
`interval-months`, the level fetch must bypass `get_level_as_timeseries`
and hit `/levels/{id}/timeseries` directly via `cwms.api.get`. The
response must carry `source_workaround: "issue-286"` and include the
chosen `effective_date` so the agent sees which level revision was used.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import cwms
import pytest
import responses

from cwms_tools.core import levels, session
from cwms_tools.core.cache import Cache, set_cache

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


def test_seasonal_level_routes_around_wrapper_via_direct_cda(configured) -> None:
    """A seasonal level config must trigger the direct-CDA bypass."""
    seasonal_config = {
        "location-level-id": "FOSS.Elev.Inst.0.Rule Curve",
        "office-id": "SWT",
        "specified-level-id": "Rule Curve",
        "level-date": "2026-01-01T00:00:00Z",
        # The seasonal marker — presence of any of these triggers the bypass.
        "seasonal-values": [
            {"value": 1648.0, "offset-months": 0},
            {"value": 1645.0, "offset-months": 6},
        ],
        "interval-months": 12,
    }
    ts_payload = {
        "units": "ft",
        "values": [[1748793600000, 1645.0, 0]],
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        # get_location_level → returns the seasonal config.
        mocked.add(
            responses.GET,
            re.compile(rf"{API_ROOT}levels/.*"),
            json=seasonal_config,
            status=200,
        )
        # The direct-CDA bypass call to /levels/{id}/timeseries.
        # `responses` will match by URL prefix — both calls hit /levels/<...>
        # but the second one has /timeseries appended. Add it second so the
        # matcher prefers the more specific pattern.
        mocked.add(
            responses.GET,
            re.compile(rf"{API_ROOT}levels/.*/timeseries.*"),
            json=ts_payload,
            status=200,
        )

        result = levels.fetch_level_value(
            "FOSS.Elev.Inst.0.Rule Curve",
            office="SWT",
            effective_date=datetime(2026, 5, 17, tzinfo=timezone.utc),
            unit="EN",
        )

    assert result["variety"] == "seasonal"
    assert result["source_workaround"] == "issue-286"
    assert result["value"] == 1645.0
    assert "level_config" in result


def test_constant_level_does_not_trigger_workaround(configured) -> None:
    constant_config = {
        "location-level-id": "FOSS.Elev.Inst.0.Spillway Crest",
        "office-id": "SWT",
        "specified-level-id": "Spillway Crest",
        "level-date": "2026-01-01T00:00:00Z",
        "constant-value": 1675.0,
        "level-units-id": "ft",
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            re.compile(rf"{API_ROOT}levels/.*"),
            json=constant_config,
            status=200,
        )

        result = levels.fetch_level_value(
            "FOSS.Elev.Inst.0.Spillway Crest",
            office="SWT",
            effective_date=datetime(2026, 5, 17, tzinfo=timezone.utc),
        )

    assert result["variety"] == "constant"
    assert result["value"] == 1675.0
    assert result["source_workaround"] is None
