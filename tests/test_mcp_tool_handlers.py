"""Tests that exercise the MCP tool handlers end-to-end.

The handlers are thin async adapters over `core/*` — these tests drive
them through `server.call_tool` with mocked CDA traffic so the per-tool
`_shape_detail` logic and structured-error path are covered.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import cwms
import pytest
import responses

from cwms_tools.core import session
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.mcp.server import build_server

API_ROOT = "https://example.test/cwms-data/"


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


LOCATIONS = {
    "locations": [
        {
            "office-id": "SWT",
            "name": "FOSS",
            "public-name": "Foss Reservoir",
            "location-kind": "PROJECT",
            "latitude": 35.55,
            "longitude": -98.97,
            "state-initial": "OK",
        }
    ]
}

TIMESERIES = {
    "entries": [
        {"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", "last-update": "2026-05-17T18:00:00Z"},
    ]
}

LOCATION_SINGLE = {
    "office-id": "SWT",
    "name": "FOSS",
    "location-kind": "PROJECT",
    "latitude": 35.55,
    "longitude": -98.97,
    "horizontal-datum": "NAD83",
    "state-initial": "OK",
    "timezone-name": "America/Chicago",
}

PROJECT_PAYLOAD = {"location": LOCATION_SINGLE, "authorizing-law": "FCA-1944"}


def _arm(mocked):
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
    mocked.add(responses.GET, f"{API_ROOT}locations/FOSS", json=LOCATION_SINGLE, status=200)
    mocked.add(responses.GET, f"{API_ROOT}projects/FOSS", json=PROJECT_PAYLOAD, status=200)


def _ts_payload(*, value: float, ts: datetime) -> dict:
    return {
        "name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        "units": "ft",
        "value-columns": [
            {"name": "date-time"},
            {"name": "value"},
            {"name": "quality-code"},
        ],
        "values": [[int(ts.timestamp() * 1000), value, 0]],
    }


def _arm_value(mocked, *, value: float, ts: datetime):
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{API_ROOT}timeseries.*"),
        json=_ts_payload(value=value, ts=ts),
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, arguments=args))


def _branch(structured: dict | None) -> dict:
    """Tolerate FastMCP's `{result: {...}}` wrapper for union return types."""
    if structured is None:
        return {}
    return structured.get("result", structured)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def test_search_places_handler(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = _call(server, "cwms_search_places", {"query": "FOSS", "office": "SWT"})
    payload = _branch(result.structured_content)
    assert payload["results"][0]["name"] == "FOSS"
    # M9 envelope: every successful task response must carry source.fingerprint.
    assert "source" in payload
    assert "fingerprint" in payload["source"]
    assert len(payload["source"]["fingerprint"]) == 64


def test_describe_place_handler_strips_in_summary(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = _call(server, "cwms_describe_place", {"office": "SWT", "name": "FOSS"})
    payload = _branch(result.structured_content)
    # Summary mode keeps only the triage subset of the Location DTO.
    assert set(payload["location"].keys()) <= {
        "office-id",
        "name",
        "location-kind",
        "latitude",
        "longitude",
        "public-name",
        "long-name",
        "horizontal-datum",
        "state-initial",
        "nearest-city",
        "timezone-name",
    }


def test_list_parameters_handler(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm(mocked)
        result = _call(server, "cwms_list_parameters", {"office": "SWT", "name": "FOSS"})
    payload = _branch(result.structured_content)
    assert payload["ts_count"] == 1
    assert payload["by_publisher"][0]["publisher"] == "Ccp-Rev"


def test_browse_region_handler_rejects_partial_bbox(configured) -> None:
    server = build_server()
    result = _call(
        server,
        "cwms_browse_region",
        {"office": "SWT", "south": 30.0, "north": 40.0},
    )
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    err = payload["error"]
    assert err["code"] == "usage_error"
    assert err["field"] == "bbox"
    # Pre-`_safe` manual branches now flow through the full envelope.
    assert err["offending_value"] == {
        "south": 30.0,
        "west": None,
        "north": 40.0,
        "east": None,
    }
    assert err["hint"] == "Pass all four bbox edges or omit bbox entirely."
    assert err["request_id"]
    assert "source" in err
    assert "protocol_request_id" not in err  # absent outside a real client session


def test_browse_region_handler_returns_ghost_office_for_nwo(configured) -> None:
    server = build_server()
    result = _call(server, "cwms_browse_region", {"office": "NWO"})
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ghost_office"


def test_browse_region_handler_rejects_negative_limit(configured) -> None:
    """A negative limit must return the in-band usage_error envelope, not crash:
    core raises a plain ValueError that `_safe` (CwmsToolsError-only) won't catch,
    so the handler validates it up front."""
    server = build_server()
    result = _call(server, "cwms_browse_region", {"office": "SWT", "limit": -1})
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert payload["error"]["field"] == "limit"


def test_search_places_handler_rejects_negative_limit(configured) -> None:
    server = build_server()
    result = _call(server, "cwms_search_places", {"query": "x", "office": "SWT", "limit": -1})
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert payload["error"]["field"] == "limit"


def test_get_value_handler(configured) -> None:
    server = build_server()
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = _call(
            server,
            "cwms_get_value",
            {"office": "SWT", "name": "FOSS", "parameter": "Elev"},
        )
    payload = _branch(result.structured_content)
    assert payload["value"] == 1648.21
    assert payload["publisher"] == "Ccp-Rev"
    # Summary mode strips chatty fields from per-threshold rows.
    assert all(
        "level_id" not in t and "source_workaround" not in t
        for t in payload.get("thresholds_active", [])
    )


def test_get_value_handler_rounds_conversion_noise(configured) -> None:
    """Issue #45: the noisy float is rounded in the structured content that
    FastMCP serializes from the Pydantic response model."""
    server = build_server()
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=68.55000000000001, ts=ts)
        result = _call(
            server,
            "cwms_get_value",
            {"office": "SWT", "name": "FOSS", "parameter": "Elev"},
        )
    payload = _branch(result.structured_content)
    assert payload["value"] == 68.55


def test_get_history_handler_rejects_bad_begin_iso(configured) -> None:
    """A bad `begin_iso` reports `field == "begin_iso"`, not the lumped
    `"begin_iso/end_iso"` placeholder the previous manual envelope used."""
    server = build_server()
    result = _call(
        server,
        "cwms_get_history",
        {
            "office": "SWT",
            "name": "FOSS",
            "parameter": "Elev",
            "begin_iso": "not-a-date",
            "end_iso": "2026-05-17T19:00:00Z",
        },
    )
    payload = _branch(result.structured_content)
    err = payload["error"]
    assert err["code"] == "invalid_field"
    assert err["field"] == "begin_iso"
    assert err["offending_value"] == "not-a-date"
    assert "RFC3339" in err["hint"]
    assert err["request_id"]
    assert "source" in err


def test_get_value_handler_rejects_unknown_unit(configured) -> None:
    """`unit` is `Literal["EN", "SI"]`. FastMCP/pydantic validates the
    argument before the tool body runs, so the schema itself rejects
    invalid values (Codex review F5)."""
    from pydantic import ValidationError

    server = build_server()
    with pytest.raises(ValidationError) as excinfo:
        _call(
            server,
            "cwms_get_value",
            {"office": "SWT", "name": "FOSS", "parameter": "Elev", "unit": "bogus"},
        )
    msg = str(excinfo.value)
    assert "unit" in msg
    assert "'EN'" in msg and "'SI'" in msg


def test_get_history_handler_rejects_bad_end_iso(configured) -> None:
    """Symmetric: bad `end_iso` is reported separately. Splitting the two
    parses means the agent always knows which field to fix."""
    server = build_server()
    result = _call(
        server,
        "cwms_get_history",
        {
            "office": "SWT",
            "name": "FOSS",
            "parameter": "Elev",
            "begin_iso": "2026-05-17T17:00:00Z",
            "end_iso": "still-not",
        },
    )
    payload = _branch(result.structured_content)
    err = payload["error"]
    assert err["code"] == "invalid_field"
    assert err["field"] == "end_iso"
    assert err["offending_value"] == "still-not"


def test_get_history_handler_returns_values(configured) -> None:
    server = build_server()
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = _call(
            server,
            "cwms_get_history",
            {
                "office": "SWT",
                "name": "FOSS",
                "parameter": "Elev",
                "begin_iso": "2026-05-17T17:00:00Z",
                "end_iso": "2026-05-17T19:00:00Z",
            },
        )
    payload = _branch(result.structured_content)
    assert payload["value_count"] == 1
    # In summary mode quality codes are None and are stripped by CompactDumpMixin.
    assert "quality" not in payload["values"][0]


def test_get_profile_handler_returns_sorted_profile(configured, monkeypatch) -> None:
    """#26/#27: cwms_get_profile reads a whole depth string, sorted shallow→deep
    with structured depth, and (summary) strips the per-sensor ts_id."""
    from cwms_tools.core import values

    monkeypatch.setattr(
        values,
        "get_profile",
        lambda *a, **k: {
            "office_id": "NWDP",
            "name": "GWLW_S1",
            "parameter": "Temp-Water",
            "unit": "EN",
            "sensor_count": 1,
            "profile": [
                {
                    "name": "GWLW_S1-D3,0ft",
                    "depth": {"value": 3.0, "unit": "ft"},
                    "value": 67.1,
                    "unit": "EN",
                    "timestamp": "2026-05-17T18:00:00Z",
                    "publisher": "IRIDIUM-REV",
                    "ts_id": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV",
                }
            ],
        },
    )
    server = build_server()
    result = _call(
        server,
        "cwms_get_profile",
        {"office": "NWDP", "name": "GWLW_S1", "parameter": "Temp-Water"},
    )
    payload = _branch(result.structured_content)
    assert payload["sensor_count"] == 1
    assert payload["profile"][0]["depth"] == {"value": 3.0, "unit": "ft"}
    assert "ts_id" not in payload["profile"][0]  # stripped in summary mode


def test_publishers_for_parameter_handler(configured) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES, status=200)
        result = _call(
            server,
            "cwms_publishers_for_parameter",
            {"parameter": "Elev", "offices": ["SWT"]},
        )
    payload = _branch(result.structured_content)
    assert any(p["publisher"] == "Ccp-Rev" for p in payload["publishers"])
    assert payload["coverage"]["complete"] is True


def test_search_places_handler_returns_ghost_office_for_nwo(configured) -> None:
    server = build_server()
    result = _call(server, "cwms_search_places", {"query": "Bear Creek", "office": "NWO"})
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ghost_office"


def test_search_places_handler_surfaces_repair_hint_for_empty_scope(configured) -> None:
    """#24: the empty-scope `repair_hint` must survive detail-shaping + Pydantic
    validation in the MCP adapter, not just exist at the core level. A bare-name
    search with no office and a cold cache resolves to empty scope (no upstream
    call) and should carry the structured hint in the tool's response."""
    server = build_server()
    result = _call(server, "cwms_search_places", {"query": "Gas Works", "parameter": "Temp-Water"})
    payload = _branch(result.structured_content)
    assert payload["ok"] is True
    hint = payload["repair_hint"]
    assert hint["reason"] == "no_offices_in_scope"
    assert hint["tool"] == "cwms_search_places"
    assert hint["args"]["query"] == "Gas Works"
    assert hint["args"]["office"]  # non-empty curated office list


def test_search_places_tool_exposes_cursor_in_schema():
    async def go():
        mcp = build_server()
        return {t.name: t for t in await mcp.list_tools()}

    tools = asyncio.run(go())
    assert "cursor" in tools["cwms_search_places"].to_mcp_tool().inputSchema["properties"]
    assert "cursor" in tools["cwms_browse_region"].to_mcp_tool().inputSchema["properties"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            "cwms_get_history",
            {
                "office": "SWT",
                "name": "FOSS",
                "parameter": "Elev",
                "begin_iso": "not-a-date",
                "end_iso": "2026-06-01T00:00:00Z",
            },
        ),
        (
            "cwms_get_history",
            {
                "office": "SWT",
                "name": "FOSS",
                "parameter": "Elev",
                "begin_iso": "2026-05-17T00:00:00Z",
                "end_iso": "not-a-date",
            },
        ),
        ("cwms_browse_region", {"office": "SWT", "south": 1.0}),
        ("cwms_browse_region", {"office": "SWT", "limit": -1}),
        ("cwms_search_places", {"query": "x", "office": "SWT", "limit": -1}),
        ("cwms_search_places", {"query": "x", "office": "NWO"}),
    ],
)
def test_error_responses_carry_source_fingerprint(configured, tool, args) -> None:
    """M9 envelope rule applies to errors too: source.fingerprint on every response,
    including the pre-_safe guard paths (bad RFC3339, partial bbox, negative limit)."""
    server = build_server()
    result = _call(server, tool, args)
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["source"]["fingerprint"] is not None
    assert len(payload["error"]["source"]["fingerprint"]) == 64


ERROR_PROVOCATIONS = [
    ("cwms_browse_region", {"office": "SWT", "south": 1.0}),
    ("cwms_browse_region", {"office": "SWT", "limit": -1}),
    ("cwms_search_places", {"query": "x", "office": "SWT", "limit": -1}),
    ("cwms_search_places", {"query": "x", "office": "NWO"}),
    (
        "cwms_get_history",
        {
            "office": "SWT",
            "name": "FOSS",
            "parameter": "Elev",
            "begin_iso": "not-a-date",
            "end_iso": "2026-06-01T00:00:00Z",
        },
    ),
    ("cwms_get_overview_section", {"section_id": "no-such-section"}),
    # Ghost-office provocations need no HTTP mock (the NW-stub guard raises
    # locally), so they cheaply prove isError:true across the remaining handlers.
    ("cwms_describe_place", {"office": "NWO", "name": "BECR"}),
    ("cwms_list_parameters", {"office": "NWO", "name": "BECR"}),
    ("cwms_get_value", {"office": "NWO", "name": "BECR", "parameter": "Elev"}),
]


@pytest.mark.parametrize(("tool", "args"), ERROR_PROVOCATIONS)
def test_tool_failures_set_protocol_iserror_with_envelope(configured, tool, args) -> None:
    """#19: tool failures set protocol-level isError:true while still carrying the
    structured `{ok: false, error: {...}}` envelope in structuredContent. The
    envelope stays the stable, branchable contract; native isError is additive."""
    server = build_server()
    result = _call(server, tool, args)
    assert result.is_error is True
    payload = _branch(result.structured_content)
    assert payload["ok"] is False
    assert payload["error"]["code"]


def test_tool_success_does_not_set_protocol_iserror(configured) -> None:
    """Success responses keep isError falsy — the additive signal fires only on failure."""
    server = build_server()
    ts = datetime(2026, 5, 17, 18, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_value(mocked, value=1648.21, ts=ts)
        result = _call(
            server,
            "cwms_get_value",
            {"office": "SWT", "name": "FOSS", "parameter": "Elev"},
        )
    assert result.is_error is not True
    assert _branch(result.structured_content)["ok"] is True


def test_error_envelope_carries_protocol_request_id(configured) -> None:
    """protocol_request_id is set when the tool runs inside a real FastMCP request context.

    Direct server.call_tool() has no active context, so we drive the call through
    an in-memory fastmcp.Client to establish a real JSON-RPC session.  The Client
    supplies the JSON-RPC request id (a string like "1") that the server echoes back.
    """
    from fastmcp import Client

    server = build_server()

    async def _go():
        async with Client(server) as client:
            # raise_on_error=False: the failure now sets protocol isError:true (#19),
            # so the Client would otherwise raise. We still inspect the envelope.
            return await client.call_tool(
                "cwms_get_overview_section",
                {"section_id": "no-such-section"},
                raise_on_error=False,
            )

    result = asyncio.run(_go())
    assert result.is_error is True
    # The fastmcp Client returns its own result wrapper; read structured_content
    # off it directly (shape-agnostic — we only rely on is_error/structured_content).
    payload = result.structured_content or {}
    payload = payload.get("result", payload)
    assert payload["ok"] is False
    # Additive field: present because the in-memory Client establishes a real
    # request context so get_context().request_id is available.
    assert payload["error"].get("protocol_request_id")


def test_semantic_nulls_survive_fastmcp_wire_serialization() -> None:
    import pydantic_core

    from cwms_tools.core.models import SourceMeta, StatusClass, ValueWithContextResponse

    resp = ValueWithContextResponse(
        ts_id="X.Elev.Inst.1Hour.0.Best",
        office_id="SWT",
        location="X",
        parameter="Elev",
        publisher=None,
        value=None,
        unit="ft",
        timestamp=None,
        status_class=StatusClass.UNKNOWN,
        thresholds_active=[],
        source=SourceMeta(fingerprint="f" * 64),
    )
    wire = pydantic_core.to_jsonable_python(resp)
    assert wire["value"] is None and wire["timestamp"] is None
    assert "publisher" not in wire
