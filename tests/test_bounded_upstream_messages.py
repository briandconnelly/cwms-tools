"""Upstream exception text must not leak into agent-facing messages (#136).

`ApiError.__str__` embeds the full request URL and response body, which can
run to kilobytes. `core/catalog.py` and `core/upstream.py` (#131) already
build bounded messages from operation + endpoint + status; these tests pin the
same contract on the location, project, and level paths: a long, distinctive
marker planted in the URL and body never reaches the CLI or MCP output, the
error code is unchanged, and the original exception stays on `__cause__`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import cwms
import pytest
import requests
from cwms.api import ApiError
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import levels, locations, projects, session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.mcp.server import build_server

API_ROOT = "https://example.test/cwms-data/"
URL_MARKER = "URLLEAK" * 400
BODY_MARKER = "BODYLEAK" * 400

runner = CliRunner()


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", API_ROOT)
    monkeypatch.delenv("_CWMS_TOOLS_NO_CACHE", raising=False)
    session._state["config"] = None
    cwms.init_session(api_root=API_ROOT, pool_connections=4)
    session.configure_session()
    cache = Cache(directory=tmp_path / "cache")
    set_cache(cache)
    yield
    cache.close()
    set_cache(None)
    session._state["config"] = None


def _api_error(status: int) -> ApiError:
    response = requests.Response()
    response.status_code = status
    response._content = BODY_MARKER.encode()
    response.url = f"{API_ROOT}x?q={URL_MARKER}"
    return ApiError(response)


def _assert_no_leak(text: str) -> None:
    assert "URLLEAK" not in text
    assert "BODYLEAK" not in text


def _mcp(name: str, args: dict) -> tuple[bool, dict, str]:
    result = asyncio.run(build_server().call_tool(name, arguments=args))
    structured = result.structured_content or {}
    raw = json.dumps(structured) + "".join(getattr(c, "text", "") for c in result.content)
    return bool(result.is_error), structured.get("result", structured), raw


def _raiser(exc: BaseException):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


# --------------------------------------------------------------------------
# Location: GET /locations/{name}
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "temporary"),
    [(404, "not_found", False), (500, "upstream_error", True), (400, "upstream_error", False)],
)
def test_location_api_error_is_bounded_on_both_surfaces(
    configured, monkeypatch, status, code, temporary
) -> None:
    monkeypatch.setattr(locations, "get_location", _raiser(_api_error(status)))

    is_error, payload, raw = _mcp("cwms_describe_place", {"office": "SWT", "name": "FOSS"})
    assert is_error is True
    assert payload["error"]["code"] == code
    assert payload["error"]["temporary"] is temporary
    _assert_no_leak(raw)

    result = runner.invoke(app, ["place", "describe", "SWT/FOSS"])
    assert result.exit_code != 0
    _assert_no_leak(result.stdout + result.stderr)
    assert code in result.stdout + result.stderr


def test_location_not_found_keeps_name_details_and_cause(configured, monkeypatch) -> None:
    exc = _api_error(404)
    monkeypatch.setattr(locations, "get_location", _raiser(exc))
    with pytest.raises(CwmsToolsError) as info:
        locations.get_one("SWT", "FOSS", use_cache=False)
    env = info.value.envelope
    assert env.code is ErrorCode.NOT_FOUND
    assert env.details is not None and env.details.value == "FOSS"
    assert "SWT/FOSS" in env.message
    assert info.value.__cause__ is exc
    _assert_no_leak(env.message)


def test_location_transport_error_is_bounded(configured, monkeypatch) -> None:
    exc = requests.exceptions.ConnectionError(f"{URL_MARKER} {BODY_MARKER}")
    monkeypatch.setattr(locations, "get_location", _raiser(exc))
    with pytest.raises(CwmsToolsError) as info:
        locations.get_one("SWT", "FOSS", use_cache=False)
    env = info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "ConnectionError" in env.message
    assert info.value.__cause__ is exc
    _assert_no_leak(env.message)


# --------------------------------------------------------------------------
# Project: GET /projects/{office}/{name} (5xx path raises; 4xx degrades)
# --------------------------------------------------------------------------


def test_project_5xx_is_bounded_on_both_surfaces(configured, monkeypatch) -> None:
    monkeypatch.setattr(locations, "get_location", lambda **_k: {"name": "FOSS"})
    monkeypatch.setattr(projects, "_upstream_get_project", _raiser(_api_error(503)))

    is_error, payload, raw = _mcp("cwms_describe_place", {"office": "SWT", "name": "FOSS"})
    assert is_error is True
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["temporary"] is True
    assert payload["error"]["source"]["endpoints_called"] == ["/projects/SWT/FOSS"]
    _assert_no_leak(raw)

    result = runner.invoke(app, ["place", "describe", "SWT/FOSS"])
    assert result.exit_code != 0
    _assert_no_leak(result.stdout + result.stderr)
    assert "upstream_error" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "exc",
    [_api_error(500), requests.exceptions.ReadTimeout(f"{URL_MARKER} {BODY_MARKER}")],
    ids=["api-error", "transport"],
)
def test_project_failure_message_is_bounded_and_keeps_cause(configured, monkeypatch, exc) -> None:
    monkeypatch.setattr(projects, "_upstream_get_project", _raiser(exc))
    with pytest.raises(CwmsToolsError) as info:
        projects.get_one("SWT", "FOSS", use_cache=False)
    env = info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "SWT/FOSS" in env.message
    assert info.value.__cause__ is exc
    _assert_no_leak(env.message)


# --------------------------------------------------------------------------
# Levels: listing, single read, seasonal workaround. `core.values` swallows
# these into `thresholds_status: unavailable`, so they never reach a surface
# today — pin the core message so a future caller can't leak them either.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [_api_error(500), requests.exceptions.ConnectionError(f"{URL_MARKER} {BODY_MARKER}")],
    ids=["api-error", "transport"],
)
def test_list_levels_failure_is_bounded(configured, monkeypatch, exc) -> None:
    monkeypatch.setattr(levels.ll_api, "get_location_levels", _raiser(exc))
    with pytest.raises(CwmsToolsError) as info:
        levels.list_levels("SWT", level_id_mask="FOSS.Elev.*", use_cache=False)
    env = info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.source.endpoints_called == ["/levels"]
    assert "FOSS.Elev.*" in env.message
    assert info.value.__cause__ is exc
    _assert_no_leak(env.message)


def test_fetch_level_value_failure_is_bounded(configured, monkeypatch) -> None:
    exc = _api_error(500)
    monkeypatch.setattr(levels.ll_api, "get_location_level", _raiser(exc))
    with pytest.raises(CwmsToolsError) as info:
        levels.fetch_level_value(
            "FOSS.Elev.Inst.0.Top of Flood", office="SWT", effective_date=datetime.now(tz=UTC)
        )
    env = info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert "HTTP 500" in env.message
    assert info.value.__cause__ is exc
    _assert_no_leak(env.message)


def test_seasonal_workaround_failure_is_bounded(configured, monkeypatch) -> None:
    exc = _api_error(502)
    monkeypatch.setattr(levels.cwms_api, "get", _raiser(exc))
    with pytest.raises(CwmsToolsError) as info:
        levels._seasonal_workaround(
            level_id="FOSS.Elev.Inst.0.Guide Curve",
            office="SWT",
            effective_date=datetime.now(tz=UTC),
            unit="EN",
            level_payload={},
        )
    env = info.value.envelope
    assert env.code is ErrorCode.WRAPPER_BUG
    assert env.source.workaround == "issue-286"
    assert "HTTP 502" in env.message
    assert info.value.__cause__ is exc
    _assert_no_leak(env.message)
    _assert_no_leak(json.dumps(env.model_dump(mode="json")))


def test_failure_summary_names_status_class_or_unreadable_body() -> None:
    from cwms_tools.core.upstream import failure_summary

    assert failure_summary(_api_error(503)) == "HTTP 503"
    assert failure_summary(_api_error(200)) == "unreadable upstream response (HTTP 200)"
    assert failure_summary(requests.exceptions.ReadTimeout(URL_MARKER)) == "ReadTimeout"
