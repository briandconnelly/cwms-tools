"""Upstream failures must land in the structured error envelope (#131).

Before #131, `core/catalog.py` caught only `ApiError` and
`core/timeseries.fetch_window` caught nothing, so a CDA 500 on a value fetch,
a dropped connection, or a `RetryError` escaped `_safe` (MCP) and
`emit_error` (CLI) as a raw exception: no `{ok: false, error: {...}}`
envelope, no `temporary` flag. These drive both surfaces through the real
cwms-python client with mocked HTTP.
"""

from __future__ import annotations

import asyncio
import json
import re

import cwms
import cwms.catalog.catalog as catalog_api
import pytest
import requests
import responses
from cwms.api import ApiError
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.upstream import upstream_call
from cwms_tools.mcp.server import build_server

API_ROOT = "https://example.test/cwms-data/"

runner = CliRunner()

TS_CATALOG = {"entries": [{"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"}]}


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


def _mcp(name: str, args: dict) -> tuple[bool, dict]:
    result = asyncio.run(build_server().call_tool(name, arguments=args))
    structured = result.structured_content or {}
    return bool(result.is_error), structured.get("result", structured)


def _arm_value_500(mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{API_ROOT}timeseries.*"),
        status=500,
        body='{"message":"System Error","incidentIdentifier":"abc-123"}',
    )


def _arm_catalog_connection_error(mocked) -> None:
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/LOCATIONS",
        body=requests.exceptions.ConnectionError("connection refused"),
    )


# --------------------------------------------------------------------------
# Value fetch: a CDA 500 on /timeseries
# --------------------------------------------------------------------------


def test_mcp_get_value_timeseries_500_returns_envelope(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value_500(mocked)
        is_error, payload = _mcp(
            "cwms_get_value", {"office": "SWT", "name": "FOSS", "parameter": "Elev"}
        )
    assert is_error is True
    assert payload["ok"] is False
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["temporary"] is True
    assert payload["error"]["source"]["endpoints_called"] == ["/timeseries"]


def test_cli_value_get_timeseries_500_reports_per_item_envelope(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value_500(mocked)
        result = runner.invoke(app, ["value", "get", "SWT/FOSS/Elev"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    item = payload["results"][0]
    assert item["ok"] is False
    assert item["error"]["code"] == "upstream_error"
    assert item["error"]["temporary"] is True


def test_profile_degrades_a_sensor_whose_fetch_500s(configured, monkeypatch) -> None:
    """`get_profile` degrades per-sensor `CwmsToolsError`s; an unwrapped
    `ApiError` used to sink the whole profile instead."""
    from cwms_tools.core import values

    def boom(*_args, **_kwargs):
        raise ApiError(_fake_response(500))

    monkeypatch.setattr(values.timeseries.ts_api, "get_timeseries", boom)
    monkeypatch.setattr(
        values.timeseries, "require_canonical_ts_id", lambda *_a, **_k: "X.Temp-Water.Inst.0.0.R"
    )
    entry = values._profile_entry(
        "SWT", "X-D1m", "Temp-Water", {"value": 1.0}, window=values.timedelta(hours=1), unit="EN"
    )
    assert entry["value"] is None
    assert entry["error"] == "upstream_error"


# --------------------------------------------------------------------------
# Empty singleton body (#137): both surfaces report the same envelope
# --------------------------------------------------------------------------


def _arm_empty_location(mocked, status: int = 200) -> None:
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", body="", status=status)


def test_mcp_describe_place_empty_location_body_returns_envelope(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_empty_location(mocked)
        is_error, payload = _mcp("cwms_describe_place", {"office": "SWT", "name": "FOSS"})
    assert is_error is True
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["temporary"] is True
    assert payload["error"]["source"]["endpoints_called"] == ["/locations/FOSS"]
    # `name` is a real cwms_describe_place argument, so MCP keeps it.
    assert payload["error"]["details"]["field"] == "name"


@pytest.mark.parametrize(
    ("status", "code"), [(200, "upstream_error"), (404, "not_found")], ids=["empty", "404"]
)
def test_cli_place_describe_location_failure_names_spec(configured, status, code) -> None:
    """`place describe` has no `name` argument; the retryable field is `spec`."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_empty_location(mocked, status=status)
        result = runner.invoke(app, ["place", "describe", "SWT/FOSS"])
    assert result.exit_code != 0
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == code
    assert envelope["error"]["source"]["endpoints_called"] == ["/locations/FOSS"]
    assert envelope["error"]["details"] == {"field": "spec", "value": "FOSS"}


def _arm_empty_project(mocked) -> None:
    mocked.add(
        responses.GET,
        f"{API_ROOT}locations/FOSS",
        json={"office-id": "SWT", "name": "FOSS", "location-kind": "PROJECT"},
    )
    mocked.add(responses.GET, f"{API_ROOT}projects/FOSS", body="", status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)


def test_describe_place_empty_project_body_is_partial_on_both_surfaces(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_empty_project(mocked)
        is_error, mcp_payload = _mcp("cwms_describe_place", {"office": "SWT", "name": "FOSS"})
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_empty_project(mocked)
        result = runner.invoke(app, ["place", "describe", "SWT/FOSS"])
    assert is_error is False
    assert result.exit_code == 0, result.stderr
    cli_payload = json.loads(result.stdout)
    for payload in (mcp_payload, cli_payload):
        assert payload["partial"] is True
        assert payload["partial_reasons"] == ["project_lookup_empty"]
        assert payload.get("project") is None


# --------------------------------------------------------------------------
# Catalog fetch: transport failures
# --------------------------------------------------------------------------


def test_mcp_browse_region_connection_error_returns_envelope(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog_connection_error(mocked)
        is_error, payload = _mcp("cwms_browse_region", {"office": "SWT"})
    assert is_error is True
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["temporary"] is True
    assert payload["error"]["source"]["endpoints_called"] == ["/catalog/LOCATIONS"]


def test_cli_region_browse_connection_error_writes_envelope_to_stderr(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog_connection_error(mocked)
        result = runner.invoke(app, ["region", "browse", "--office", "SWT"])
    assert result.exit_code != 0
    assert result.stdout == ""
    envelope = json.loads(result.stderr)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "upstream_error"
    assert envelope["error"]["temporary"] is True


def test_retry_error_with_non_transport_cause_is_wrapped(configured, monkeypatch) -> None:
    """cwms-python 1.1.x re-raises `RetryError` itself when the retry cause isn't
    a transport error (it used to unwrap it)."""

    def raise_retry(**_kwargs):
        raise requests.exceptions.RetryError("Max retries exceeded")

    monkeypatch.setattr(catalog_api, "get_locations_catalog", raise_retry)
    is_error, payload = _mcp("cwms_browse_region", {"office": "SWT"})
    assert is_error is True
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["temporary"] is True


# --------------------------------------------------------------------------
# The helper itself
# --------------------------------------------------------------------------


def _fake_response(
    status: int, *, body: str = "", url: str = "https://x.test/y"
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    response.url = url
    return response


def test_helper_message_omits_exception_text_and_keeps_cause() -> None:
    exc = ApiError(_fake_response(500, body="SECRET_BODY", url="https://x.test/" + "q" * 3000))
    with pytest.raises(CwmsToolsError) as ex_info, upstream_call("/timeseries", operation="fetch"):
        raise exc
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "SECRET_BODY" not in env.message
    assert "qqqq" not in env.message
    assert len(env.message) < 200
    assert ex_info.value.__cause__ is exc


def test_helper_maps_404_to_not_found_and_429_to_rate_limited() -> None:
    with pytest.raises(CwmsToolsError) as nf, upstream_call("/timeseries", operation="fetch"):
        raise ApiError(_fake_response(404))
    assert nf.value.envelope.code is ErrorCode.NOT_FOUND
    with pytest.raises(CwmsToolsError) as rl, upstream_call("/timeseries", operation="fetch"):
        raise ApiError(_fake_response(429))
    assert rl.value.envelope.code is ErrorCode.RATE_LIMITED


def test_helper_names_unreadable_2xx_response() -> None:
    """1.1.x raises `ApiError` with status 200 for invalid JSON; say so rather
    than the misleading 'Upstream returned 200'."""
    with pytest.raises(CwmsToolsError) as ex_info, upstream_call("/x", operation="fetch"):
        raise ApiError(_fake_response(200))
    assert "unreadable" in ex_info.value.envelope.message
    assert ex_info.value.envelope.temporary is True


def test_helper_does_not_swallow_programming_errors() -> None:
    with pytest.raises(KeyError), upstream_call("/x", operation="fetch"):
        raise KeyError("bug")
