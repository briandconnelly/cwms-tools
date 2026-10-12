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


def test_value_get_rounds_conversion_noise(configured) -> None:
    """Issue #45: a noisy unit-converted value is rounded in the CLI JSON
    output (the CLI path bypasses the Pydantic models, so render.emit rounds)."""
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=68.55000000000001, ts=ts)
        result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev"])
    assert result.exit_code == 0, result.stdout
    # The raw JSON text must not carry the IEEE-754 tail.
    assert "68.55000000000001" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["results"][0]["data"]["value"] == 68.55


def test_value_get_with_status_runs_classification(configured) -> None:
    """`--with-status` opts into the slow threshold lookup. Hitting the
    /levels endpoint is required; the response reports the lookup ran to
    completion via level_lookup_status."""
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)
        result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev", "--with-status"])
        levels_calls = [c for c in mocked.calls if "/levels" in (c.request.url or "")]
        assert levels_calls, "--with-status must hit the levels endpoint"
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    item = payload["results"][0]
    assert item["data"]["level_lookup_status"] == "unavailable"
    assert item["data"]["status_class"] == "unknown"


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
    # The CLI names a runnable command, not the MCP tool (#96).
    assert failed["error"]["repair"]["tool"] == "cwms-tools place parameters"
    assert failed["error"]["repair"]["arguments"] == {"spec": "SWT/FOSS"}


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
    assert payload["error"]["details"]["field"] == "begin"  # precise field, not lumped "begin/end"


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


def test_value_history_ghost_office_names_id_spec_not_office() -> None:
    """#68 review: `value history` has no `--office` flag — the retryable
    arg for a ghost_office error is `id_spec` (the declared CLI argument,
    per `cli/commands/schema.py`), not the nonexistent "office"."""
    result = runner.invoke(
        app,
        [
            "value",
            "history",
            "NWO/FTPK/Elev",
            "--begin",
            "2026-05-17T17:00:00Z",
            "--end",
            "2026-05-17T19:00:00Z",
        ],
    )
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ghost_office"
    assert payload["error"]["details"]["field"] == "id_spec"
    # #69: repair retries `value history` with the SAME name/parameter/
    # window/unit/rollup, office rolled up into a fresh id_spec.
    # #69 review: `tool` names the actual CLI invocation with CLI flag
    # names (`begin`/`end`), not the MCP tool's `begin_iso`/`end_iso`.
    repair = payload["error"]["repair"]
    assert repair["tool"] == "cwms-tools value history"
    assert repair["arguments"] == {
        "id_spec": "NWDM/FTPK/Elev",
        "begin": "2026-05-17T17:00:00Z",
        "end": "2026-05-17T19:00:00Z",
        "unit": "EN",
        "rollup": "raw",
        "detail": "summary",
    }


def test_usage_error_writes_full_envelope_to_stderr() -> None:
    """C1/C3: whole-command usage errors emit the FULL ErrorEnvelope (with
    request_id, details.reason, details.field) to stderr — not the old
    hand-built partial dict on stdout."""
    result = runner.invoke(app, ["value", "get", "no-slashes"])
    assert result.exit_code == 2
    assert result.stdout == ""
    err = json.loads(result.stderr)["error"]
    assert err["code"] == "usage_error"
    assert err["details"]["field"] == "id"
    assert err["request_id"]  # full envelope, not the old partial shape
    assert err["details"]["reason"]


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
    # #68: this per-item error bypasses `emit_error()` (it's embedded in the
    # success-shaped batch envelope). `value get` has no `--office` flag —
    # `id_specs` (the declared CLI argument) is the retryable arg, not the
    # nonexistent "office".
    assert payload["results"][0]["error"]["details"]["field"] == "id_specs"
    # #69: repair retries `value get` with a fresh id_specs list containing
    # only THIS failed spec (not the whole original list — re-running
    # already-ok items would be wasteful/confusing), office rolled up.
    repair = payload["results"][0]["error"]["repair"]
    assert repair["tool"] == "cwms-tools value get"
    assert repair["arguments"] == {
        "id_specs": ["NWDM/FTPK/Elev"],
        "window_hours": 24,
        "unit": "EN",
        "with_status": False,
        "detail": "summary",
    }


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


def test_value_profile_ghost_office_names_id_spec_not_office() -> None:
    """#68 review: `value profile` has no `--office` flag either."""
    result = runner.invoke(app, ["value", "profile", "NWO/GWLW_S1/Temp-Water"])
    assert result.exit_code == 12  # GHOST exit
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ghost_office"
    assert payload["error"]["details"]["field"] == "id_spec"
    repair = payload["error"]["repair"]
    assert repair["tool"] == "cwms-tools value profile"
    assert repair["arguments"] == {
        "id_spec": "NWDM/GWLW_S1/Temp-Water",
        "window_hours": 24,
        "unit": "EN",
        "detail": "summary",
    }


def test_value_get_batch_item_resolves_repair_intent_into_id_specs(monkeypatch) -> None:
    from cwms_tools.core import values
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent

    def boom(*a, **k):
        raise CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            "no",
            field="name",
            value="Foss",
            repair=RepairHint(next_step="search", tool="cwms_search_places", arguments={}),
            repair_intent=RepairIntent("retry_with_catalog_id", {"name": "FOSS"}),
        )

    monkeypatch.setattr(values, "get_value", boom)
    result = runner.invoke(app, ["value", "get", "SWT/Foss/Elev", "--unit", "SI", "--with-status"])
    assert result.exit_code == 3
    item = json.loads(result.stdout)["results"][0]
    repair = item["error"]["repair"]
    assert repair["tool"] == "cwms-tools value get"
    assert repair["arguments"]["id_specs"] == ["SWT/FOSS/Elev"]
    assert repair["arguments"]["unit"] == "SI" and repair["arguments"]["with_status"] is True
    # The diagnostic names the CLI's real positional, not the MCP arg (#68 rule).
    assert item["error"]["details"]["field"] == "id_specs"


def test_value_get_ghost_location_exits_12(monkeypatch) -> None:
    from cwms_tools.core import values
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

    def boom(*a, **k):
        raise CwmsToolsError.of(
            ErrorCode.GHOST_LOCATION,
            "ghost",
            field="name",
            value="X",
            repair=RepairHint(next_step="s", tool="cwms_search_places", arguments={}),
        )

    monkeypatch.setattr(values, "get_value", boom)
    result = runner.invoke(app, ["value", "get", "SWT/X/Elev"])
    assert result.exit_code == 12
    assert json.loads(result.stdout)["results"][0]["error"]["code"] == "ghost_location"


# --------------------------------------------------------------------------
# CLI repairs name runnable commands (#96): real core errors, mocked catalogs
# --------------------------------------------------------------------------

GWLW_LOCATIONS = {
    "entries": [
        {"office": "NWDP", "name": "GWLW_S1", "latitude": 47.65, "longitude": -122.33},
        {"office": "NWDP", "name": "GWLW_S1-D3,0ft", "latitude": 47.6501, "longitude": -122.33},
        {"office": "NWDP", "name": "LONELY", "latitude": 46.0, "longitude": -120.0},
    ]
}
GWLW_TS = {"entries": [{"name": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"}]}


def _arm_gwlw(mocked, *, ts_for_location=None) -> None:
    # The scoped ts read carries `like=`; the office-wide enrichment does not.
    mocked.add(
        responses.GET,
        re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
        json=ts_for_location if ts_for_location is not None else {"entries": []},
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=GWLW_TS, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=GWLW_LOCATIONS, status=200)


def test_value_get_case_c_search_repair_is_place_search(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_gwlw(mocked)
        result = runner.invoke(app, ["value", "get", "NWDP/LONELY/Elev"])
    item = json.loads(result.stdout)["results"][0]
    assert item["error"]["code"] == "ghost_location"
    assert item["error"]["repair"]["tool"] == "cwms-tools place search"
    assert item["error"]["repair"]["arguments"] == {"query": "LONELY", "office": ["NWDP"]}


def test_value_get_depth_ghost_profile_alternative_is_value_profile(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_gwlw(mocked)
        result = runner.invoke(app, ["value", "get", "NWDP/GWLW_S1/Temp-Water"])
    repair = json.loads(result.stdout)["results"][0]["error"]["repair"]
    assert repair["tool"] == "cwms-tools value get"
    assert repair["arguments"]["id_specs"] == ["NWDP/GWLW_S1-D3,0ft/Temp-Water"]
    assert repair["alternative"] == (
        "cwms-tools value profile NWDP/GWLW_S1/Temp-Water reads every depth of this "
        "string in one call."
    )
    assert "cwms_get_profile" not in repair["alternative"]


def test_value_history_unknown_office_repair_is_offices_command(
    configured, validating_directory
) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=[{"office-id": "SWT", "type": "DIS"}])
        result = runner.invoke(
            app,
            [
                "value",
                "history",
                "XYZ/FOSS/Elev",
                "--begin",
                "2026-05-17T00:00:00Z",
                "--end",
                "2026-05-18T00:00:00Z",
            ],
        )
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_field"
    assert error["repair"]["tool"] == "cwms-tools offices"
    assert error["repair"]["arguments"] == {}


def test_value_history_wrong_case_repair_arguments_are_pinned(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_gwlw(mocked)
        result = runner.invoke(
            app,
            [
                "value",
                "history",
                "NWDP/gwlw_s1/Temp-Water",
                "--begin",
                "2026-05-17T00:00:00Z",
                "--end",
                "2026-05-18T00:00:00Z",
                "--rollup",
                "daily",
            ],
        )
    assert result.exit_code == 3
    repair = json.loads(result.stderr)["error"]["repair"]
    assert repair["tool"] == "cwms-tools value history"
    assert repair["next_step"] == "retry_with_catalog_id"
    assert repair["arguments"] == {
        "begin": "2026-05-17T00:00:00Z",
        "end": "2026-05-18T00:00:00Z",
        "unit": "EN",
        "rollup": "daily",
        "detail": "summary",
        "id_spec": "NWDP/GWLW_S1/Temp-Water",
    }


def test_profile_wrong_case_parent_repairs_on_each_surface(configured) -> None:
    """#163/#96: the same core not_found replays as `cwms_get_profile` on MCP and
    as `value profile` on the CLI, each with the catalog id."""
    import asyncio

    from cwms_tools.mcp.server import build_server

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_gwlw(mocked, ts_for_location=GWLW_TS)
        res = asyncio.run(
            build_server().call_tool(
                "cwms_get_profile",
                arguments={"office": "NWDP", "name": "gwlw_s1", "parameter": "Temp-Water"},
            )
        )
        cli = runner.invoke(app, ["value", "profile", "NWDP/gwlw_s1/Temp-Water"])
    structured = res.structured_content or {}
    mcp_err = structured.get("result", structured)["error"]
    assert mcp_err["code"] == "not_found"
    assert mcp_err["repair"]["tool"] == "cwms_get_profile"
    assert mcp_err["repair"]["arguments"]["name"] == "GWLW_S1"
    assert mcp_err["repair"]["arguments"]["office"] == "NWDP"
    assert mcp_err["repair"]["arguments"]["parameter"] == "Temp-Water"
    cli_err = json.loads(cli.stderr)["error"]
    assert cli.exit_code == 3
    assert cli_err["code"] == "not_found"
    assert cli_err["repair"]["tool"] == "cwms-tools value profile"
    assert cli_err["repair"]["arguments"] == {
        "window_hours": 24,
        "unit": "EN",
        "detail": "summary",
        "id_spec": "NWDP/GWLW_S1/Temp-Water",
    }


def test_localize_repair_passes_unknown_tools_through() -> None:
    from cwms_tools.cli.render import localize_repair
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

    hint = RepairHint(next_step="x", tool="cwms_future_tool", arguments={"a": 1})
    err = CwmsToolsError.of(ErrorCode.NOT_FOUND, "no", repair=hint)
    assert localize_repair(err).envelope.repair == hint
    # A repair already naming a CLI command is untouched too.
    cli_hint = RepairHint(next_step="y", tool="cwms-tools place search", arguments={"query": "q"})
    err = CwmsToolsError.of(ErrorCode.NOT_FOUND, "no", repair=cli_hint)
    assert localize_repair(err).envelope.repair == cli_hint


def test_attach_repair_intent_spec_mode_keeps_non_positional_overrides() -> None:
    """T9: an override that is part of the positional is re-joined into it; any
    other override becomes an ordinary option instead of being dropped."""
    from cwms_tools.cli.render import attach_repair_intent
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairIntent

    err = CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        "no",
        repair_intent=RepairIntent("retry", {"parameter": "Elev", "window_hours": 48}),
    )
    attach_repair_intent(
        err,
        tool="cwms-tools value get",
        args={"window_hours": 24, "unit": "EN"},
        spec_key="id_specs",
        spec_fields={"office": "SWT", "name": "FOSS", "parameter": "elev"},
        spec_as_list=True,
    )
    assert err.envelope.repair is not None
    assert err.envelope.repair.arguments == {
        "window_hours": 48,
        "unit": "EN",
        "id_specs": ["SWT/FOSS/Elev"],
    }


@pytest.mark.parametrize(
    ("begin", "end", "code", "field"),
    [
        ("2026-10-05", "2026-10-06T00:00:00Z", "invalid_field", "begin"),
        ("2026-10-06T00:00:00Z", "2026-10-05T00:00:00Z", "usage_error", "end"),
    ],
)
def test_value_history_window_validation(begin, end, code, field) -> None:
    args = ["value", "history", "SWT/FOSS/Elev", "--begin", begin, "--end", end]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        result = runner.invoke(app, args)
        assert len(mocked.calls) == 0
    assert result.exit_code == 2
    err = json.loads(result.stderr)["error"]
    assert err["code"] == code
    assert err["details"]["field"] == field
