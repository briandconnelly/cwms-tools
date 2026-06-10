"""Contract test: every error code advertised in TOOL_ERROR_CODES is provable.

For each (tool, code) pair there is a provocation — concrete arguments plus
mocked upstream behavior — that makes the live handler return that code.
Set-equality per tool means: an advertised code with no fixture FAILS (the
F1 class of drift), and a provoked code that is not advertised FAILS.
"""

from __future__ import annotations

import asyncio
import re

import cwms
import pytest
import responses

from cwms_tools.core import session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import ErrorCode
from cwms_tools.mcp.resources import TOOL_ERROR_CODES
from cwms_tools.mcp.server import build_server

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


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, arguments=args))


def _error_code(result) -> str | None:
    structured = result.structured_content or {}
    payload = structured.get("result", structured)
    if payload.get("ok") is False:
        return payload["error"]["code"]
    return None


def _mock_all(mocked, status: int, json_body=None, headers=None) -> None:
    """Catch-all upstream mock: every CDA GET returns `status`."""
    mocked.add(
        responses.GET,
        re.compile(rf"{API_ROOT}.*"),
        json=json_body if json_body is not None else {"detail": "boom"},
        status=status,
        headers=headers or {},
    )


GOOD_ARGS = {
    "cwms_search_places": {"query": "FOSS", "office": "SWT"},
    "cwms_describe_place": {"office": "SWT", "name": "FOSS"},
    "cwms_list_parameters": {"office": "SWT", "name": "FOSS"},
    "cwms_browse_region": {"office": "SWT"},
    "cwms_get_value": {"office": "SWT", "name": "FOSS", "parameter": "Elev"},
    "cwms_get_history": {
        "office": "SWT",
        "name": "FOSS",
        "parameter": "Elev",
        "begin_iso": "2026-05-17T00:00:00Z",
        "end_iso": "2026-05-18T00:00:00Z",
    },
}

NW_STUB_ARGS = {
    "cwms_search_places": {"query": "x", "office": "NWO"},
    "cwms_describe_place": {"office": "NWO", "name": "BECR"},
    "cwms_list_parameters": {"office": "NWO", "name": "BECR"},
    "cwms_browse_region": {"office": "NWO"},
    "cwms_get_value": {"office": "NWO", "name": "BECR", "parameter": "Elev"},
    "cwms_get_history": {
        "office": "NWO",
        "name": "BECR",
        "parameter": "Elev",
        "begin_iso": "2026-05-17T00:00:00Z",
        "end_iso": "2026-05-18T00:00:00Z",
    },
}

# (tool, code) -> (args, upstream_status or None for purely-local provocations)
PROVOCATIONS: dict[tuple[str, str], tuple[dict, int | None]] = {}
for tool, stub_args in NW_STUB_ARGS.items():
    PROVOCATIONS[(tool, "ghost_office")] = (stub_args, None)
for tool in ("cwms_search_places", "cwms_browse_region"):
    PROVOCATIONS[(tool, "invalid_cursor")] = (
        {**GOOD_ARGS[tool], "cursor": "garbage-cursor", "limit": 5},
        None,
    )
for tool, args in GOOD_ARGS.items():
    PROVOCATIONS[(tool, "rate_limited")] = (args, 429)
    PROVOCATIONS[(tool, "upstream_error")] = (args, 500)
PROVOCATIONS[("cwms_describe_place", "not_found")] = (GOOD_ARGS["cwms_describe_place"], 404)
PROVOCATIONS[("cwms_list_parameters", "not_found")] = (GOOD_ARGS["cwms_list_parameters"], 404)
PROVOCATIONS[("cwms_get_value", "not_found")] = (GOOD_ARGS["cwms_get_value"], 404)
PROVOCATIONS[("cwms_get_history", "not_found")] = (GOOD_ARGS["cwms_get_history"], 404)
PROVOCATIONS[("cwms_get_history", "invalid_field")] = (
    {**GOOD_ARGS["cwms_get_history"], "begin_iso": "not-a-date"},
    None,
)
PROVOCATIONS[("cwms_browse_region", "usage_error")] = (
    {"office": "SWT", "south": 1.0},  # partial bbox
    None,
)
PROVOCATIONS[("cwms_get_overview_section", "not_found")] = (
    {"section_id": "no-such-section"},
    None,
)


def test_every_advertised_code_has_a_provocation() -> None:
    advertised = {(tool, code) for tool, codes in TOOL_ERROR_CODES.items() for code in codes}
    assert advertised == set(PROVOCATIONS), (
        "TOOL_ERROR_CODES and the provocation table drifted. Advertised-but-unprovable "
        f"codes: {sorted(advertised - set(PROVOCATIONS))}; provoked-but-unadvertised: "
        f"{sorted(set(PROVOCATIONS) - advertised)}"
    )


def test_advertised_codes_subset_of_enum() -> None:
    enum_values = {c.value for c in ErrorCode}
    for tool, codes in TOOL_ERROR_CODES.items():
        assert set(codes) <= enum_values, f"{tool} advertises unknown codes"


@pytest.mark.parametrize(("tool", "code"), sorted(PROVOCATIONS))
def test_provocation_returns_advertised_code(configured, tool: str, code: str) -> None:
    args, status = PROVOCATIONS[(tool, code)]
    server = build_server()
    if status is None:
        result = _call(server, tool, args)
    else:
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            _mock_all(mocked, status, headers={"Retry-After": "0"} if status == 429 else {})
            result = _call(server, tool, args)
    assert _error_code(result) == code


def test_publishers_for_parameter_absorbs_office_errors(configured) -> None:
    """This tool advertises NO error codes: per-office failures degrade into
    coverage.offices_error_skipped on an ok:true response."""
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _mock_all(mocked, 500)
        result = _call(
            server, "cwms_publishers_for_parameter", {"parameter": "Elev", "offices": ["SWT"]}
        )
    structured = result.structured_content or {}
    payload = structured.get("result", structured)
    assert payload["ok"] is True
    assert "SWT" in payload["coverage"]["offices_error_skipped"]
