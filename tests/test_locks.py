"""Opt-in lock details on `describe_place` (#134).

Driven through the real cwms-python client with mocked HTTP, using payloads
recorded from CDA on 2026-10-11 (`swt_webb_locks_list.json`,
`swt_webb_lock_en.json`). The list endpoint returns placeholder `0.0`
dimensions, so details come from one `get_lock` per lock.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import cwms
import pytest
import responses
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import locks, places, session, shaping
from cwms_tools.core.cache import Cache, get_cache, set_cache
from cwms_tools.core.models import Detail
from cwms_tools.mcp.server import build_server

from .conftest import fixture_path

API_ROOT = "https://example.test/cwms-data/"

runner = CliRunner()

LOCKS_LIST = json.loads(fixture_path("swt_webb_locks_list.json").read_text())
LOCK_EN = json.loads(fixture_path("swt_webb_lock_en.json").read_text())

WEBB_LOCATION = {
    "office-id": "SWT",
    "name": "WEBB",
    "location-kind": "PROJECT",
    "public-name": "Robert S. Kerr Lock and Dam",
    "latitude": 35.35,
    "longitude": -94.78,
    "horizontal-datum": "NAD83",
    "state-initial": "OK",
    "timezone-name": "US/Central",
}
LOCK_LOCATION = {**WEBB_LOCATION, "name": "WEBB-Lock", "location-kind": "LOCK"}
SITE_LOCATION = {**WEBB_LOCATION, "name": "GAGE1", "location-kind": "SITE"}


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


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rmock:
        yield rmock


def _arm_lock_list(mocked, *, status: int = 200, body: Any = None) -> None:
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/locks",
        json=LOCKS_LIST if body is None else body,
        status=status,
    )


def _arm_lock_detail(mocked, *, name: str = "WEBB-Lock", status: int = 200) -> None:
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/locks/{name}",
        json=LOCK_EN if status == 200 else {"message": "System Error"},
        status=status,
    )


def _arm_describe(mocked, location: dict[str, Any]) -> None:
    name = location["name"]
    mocked.add(responses.GET, f"{API_ROOT}locations/{name}", json=location, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{API_ROOT}projects/(SWT/)?{re.escape(name)}(\?.*)?$"),
        json={"location": location, "project-owner": "USACE"},
        status=200 if location["location-kind"] == "PROJECT" else 404,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)


# --------------------------------------------------------------------------
# Producer: core.locks.lookup
# --------------------------------------------------------------------------


def test_project_lists_then_fetches_each_lock(configured, mocked) -> None:
    _arm_lock_list(mocked)
    _arm_lock_detail(mocked)
    out = locks.lookup("SWT", "WEBB", "PROJECT")
    assert out["lock_lookup_status"] == "ok"
    assert "lock_upstream_status" not in out
    [lock] = out["locks"]
    assert lock["name"] == "WEBB-Lock"
    assert lock["public-name"] == "Lock"
    # Real dimensions come from the detail call, not the list's 0.0 placeholders.
    assert lock["lock-width"] == pytest.approx(110.0)
    assert lock["length-units"] == "ft"


def test_negative_sentinel_dimension_is_nulled_and_recorded(configured, mocked) -> None:
    _arm_lock_list(mocked)
    _arm_lock_detail(mocked)
    [lock] = locks.lookup("SWT", "WEBB", "PROJECT")["locks"]
    assert lock["maximum-lock-lift"] is None
    assert lock["invalid_fields"] == ["maximum-lock-lift"]
    assert lock["minimum-draft"] == 9.0


def test_zero_dimensions_are_preserved(configured, mocked) -> None:
    _arm_lock_list(mocked)
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/locks/WEBB-Lock",
        json={**LOCK_EN, "minimum-draft": 0.0, "maximum-lock-lift": 0.0},
        status=200,
    )
    [lock] = locks.lookup("SWT", "WEBB", "PROJECT")["locks"]
    assert lock["minimum-draft"] == 0.0
    assert lock["maximum-lock-lift"] == 0.0
    assert lock["invalid_fields"] == []


def test_lock_kind_fetches_itself_without_listing(configured, mocked) -> None:
    _arm_lock_detail(mocked)
    out = locks.lookup("SWT", "WEBB-Lock", "lock")
    assert out["lock_lookup_status"] == "ok"
    assert [lk["name"] for lk in out["locks"]] == ["WEBB-Lock"]
    assert not [c for c in mocked.calls if c.request.url.split("?")[0].endswith("/projects/locks")]


def test_other_kinds_are_not_applicable_and_make_no_call(configured, mocked) -> None:
    for kind in ("SITE", None, ""):
        out = locks.lookup("SWT", "GAGE1", kind)
        assert out == {"lock_lookup_status": "not_applicable", "locks": []}
    assert len(mocked.calls) == 0


def test_project_with_no_locks_is_ok_and_empty(configured, mocked) -> None:
    _arm_lock_list(mocked, body=[])
    assert locks.lookup("NWDP", "BON", "PROJECT") == {"lock_lookup_status": "ok", "locks": []}


def test_list_failure_is_failed_with_status(configured, mocked) -> None:
    _arm_lock_list(mocked, status=500, body={"message": "System Error"})
    out = locks.lookup("MVR", "IL02", "PROJECT")
    assert out == {"lock_lookup_status": "failed", "locks": [], "lock_upstream_status": 500}


def test_detail_failure_on_only_lock_is_failed(configured, mocked) -> None:
    _arm_lock_detail(mocked, status=500)
    out = locks.lookup("LRN", "WEBB-Lock", "LOCK")
    assert out["lock_lookup_status"] == "failed"
    assert out["locks"] == [{"name": "WEBB-Lock", "error": "upstream_error"}]
    assert out["lock_upstream_status"] == 500


def test_detail_failure_on_one_of_two_locks_is_partial(configured, mocked) -> None:
    second = json.loads(json.dumps(LOCKS_LIST[0]))
    second["location"]["name"] = "WEBB-Lock2"
    _arm_lock_list(mocked, body=[LOCKS_LIST[0], second])
    _arm_lock_detail(mocked)
    _arm_lock_detail(mocked, name="WEBB-Lock2", status=500)
    out = locks.lookup("SWT", "WEBB", "PROJECT")
    assert out["lock_lookup_status"] == "partial"
    assert [lk.get("error") for lk in out["locks"]] == [None, "upstream_error"]
    assert out["lock_upstream_status"] == 500


def test_transport_failure_is_failed_without_status(configured, mocked) -> None:
    import requests

    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/locks",
        body=requests.exceptions.ConnectionError("refused"),
    )
    out = locks.lookup("SWT", "WEBB", "PROJECT")
    assert out == {"lock_lookup_status": "failed", "locks": []}


def test_successful_details_are_cached_failures_are_not(configured, mocked) -> None:
    _arm_lock_list(mocked)
    _arm_lock_detail(mocked)
    locks.lookup("SWT", "WEBB", "PROJECT")
    calls = len(mocked.calls)
    locks.lookup("SWT", "WEBB", "PROJECT")
    assert len(mocked.calls) == calls, "second lookup must be served from cache"

    _arm_lock_detail(mocked, name="BAD-Lock", status=500)
    locks.lookup("SWT", "BAD-Lock", "LOCK")
    before = len(mocked.calls)
    locks.lookup("SWT", "BAD-Lock", "LOCK")
    assert len(mocked.calls) == before + 1, "a failure must not be cached"


def test_cached_lock_is_not_mutated_by_shaping(configured, mocked) -> None:
    _arm_lock_list(mocked)
    _arm_lock_detail(mocked)
    first = locks.lookup("SWT", "WEBB", "PROJECT")
    shaping.shape_place_detail({"locks": first["locks"]}, Detail.SUMMARY)
    again = locks.lookup("SWT", "WEBB", "PROJECT")
    assert "location" in again["locks"][0]


# --------------------------------------------------------------------------
# describe_place integration
# --------------------------------------------------------------------------


def test_describe_omits_lock_keys_by_default(configured, mocked) -> None:
    _arm_describe(mocked, WEBB_LOCATION)
    out = places.describe_place("SWT", "WEBB")
    assert not {"locks", "lock_lookup_status", "lock_upstream_status"} & out.keys()
    assert not [c for c in mocked.calls if "/projects/locks" in c.request.url]


def test_describe_with_locks_failure_marks_partial(configured, mocked) -> None:
    _arm_describe(mocked, WEBB_LOCATION)
    _arm_lock_list(mocked, status=500, body={"message": "System Error"})
    out = places.describe_place("SWT", "WEBB", include_locks=True)
    assert out["lock_lookup_status"] == "failed"
    assert out["partial"] is True
    assert "lock_lookup_failed" in out["partial_reasons"]
    # The project lookup's own status meaning is untouched.
    assert out["upstream_status"] is None


def test_describe_lock_location_keeps_project_404_and_adds_lock_reason(configured, mocked) -> None:
    _arm_describe(mocked, LOCK_LOCATION)
    _arm_lock_detail(mocked, status=500)
    out = places.describe_place("SWT", "WEBB-Lock", include_locks=True)
    assert out["partial_reasons"] == ["not_a_project", "lock_lookup_failed"]
    assert out["upstream_status"] == 404
    assert out["lock_upstream_status"] == 500


def test_describe_lock_reason_does_not_leak_into_cached_project_result(configured, mocked) -> None:
    _arm_describe(mocked, LOCK_LOCATION)
    _arm_lock_detail(mocked, status=500)
    places.describe_place("SWT", "WEBB-Lock", include_locks=True)
    plain = places.describe_place("SWT", "WEBB-Lock")
    assert plain["partial_reasons"] == ["not_a_project"]


# --------------------------------------------------------------------------
# Shaping + CLI/MCP parity
# --------------------------------------------------------------------------


def test_summary_keeps_dimensions_drops_location_and_level_links(configured, mocked) -> None:
    _arm_lock_list(mocked)
    _arm_lock_detail(mocked)
    raw = locks.lookup("SWT", "WEBB", "PROJECT")
    [lock] = shaping.shape_place_detail(raw, Detail.SUMMARY)["locks"]
    # Spelled out rather than derived from LOCK_SUMMARY_KEYS, so dropping a
    # dimension from that constant fails here instead of passing silently.
    rounded = {k: round(v, 3) if isinstance(v, float) else v for k, v in lock.items()}
    assert rounded == {
        "name": "WEBB-Lock",
        "public-name": "Lock",
        "lock-width": 110.0,
        "lock-length": 600.0,
        "normal-lock-lift": 30.0,
        "maximum-lock-lift": None,  # nulled -9999 m sentinel
        "volume-per-lockage": 1987200.0,
        "minimum-draft": 9.0,
        "length-units": "ft",
        "volume-units": "ft3",
        "elevation-units": "ft",
        "invalid_fields": ["maximum-lock-lift"],
    }
    [full] = shaping.shape_place_detail(raw, Detail.FULL)["locks"]
    assert "location" in full
    assert "high-water-upper-pool-location-level" in full


def _mcp_describe(args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(build_server().call_tool("cwms_describe_place", arguments=args))
    payload = dict((result.structured_content or {}).get("result", result.structured_content))
    payload.pop("ok", None)
    payload.pop("source", None)
    return payload


def _cli_describe(spec: str, *extra: str) -> dict[str, Any]:
    result = runner.invoke(app, ["place", "describe", spec, *extra])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


LOCK_KEYS = {"locks", "lock_lookup_status", "lock_upstream_status"}

# Every include_locks scenario the two surfaces must agree on. Each scenario's
# expectation is asserted on BOTH surfaces, so a handler that silently stops
# forwarding `include_locks` fails here instead of trivially matching its peer.
LOCK_SCENARIOS = ["omitted", "ok_populated", "ok_empty", "partial", "failed", "not_applicable"]


def _arm_lock_scenario(mocked, scenario: str) -> None:
    if scenario in ("ok_populated", "omitted"):
        _arm_lock_list(mocked)
        _arm_lock_detail(mocked)
    elif scenario == "ok_empty":
        _arm_lock_list(mocked, body=[])
    elif scenario == "partial":
        second = json.loads(json.dumps(LOCKS_LIST[0]))
        second["location"]["name"] = "WEBB-Lock2"
        _arm_lock_list(mocked, body=[LOCKS_LIST[0], second])
        _arm_lock_detail(mocked)
        _arm_lock_detail(mocked, name="WEBB-Lock2", status=500)
    elif scenario == "failed":
        _arm_lock_list(mocked, status=500, body={"message": "System Error"})


def _assert_lock_scenario(out: dict[str, Any], scenario: str, detail: str) -> None:
    if scenario == "omitted":
        assert not LOCK_KEYS & out.keys()
        assert "lock_lookup_failed" not in out["partial_reasons"]
        return
    degraded = scenario in ("partial", "failed")
    # SITE_LOCATION's project lookup is a 404, so that scenario is partial
    # (`not_a_project`) on its own; the lock reason must still be absent.
    assert out["partial"] is (degraded or scenario == "not_applicable")
    assert ("lock_lookup_failed" in out["partial_reasons"]) is degraded
    assert ("lock_upstream_status" in out) is degraded
    if degraded:
        assert out["lock_upstream_status"] == 500
    if scenario == "ok_populated":
        assert out["lock_lookup_status"] == "ok"
        [lock] = out["locks"]
    elif scenario == "partial":
        assert out["lock_lookup_status"] == "partial"
        lock, bad = out["locks"]
        assert bad == {"name": "WEBB-Lock2", "error": "upstream_error"}
    else:
        assert (
            out["lock_lookup_status"]
            == {
                "ok_empty": "ok",
                "failed": "failed",
                "not_applicable": "not_applicable",
            }[scenario]
        )
        assert out["locks"] == []
        return
    assert lock["name"] == "WEBB-Lock"
    assert lock["lock-width"] == pytest.approx(110.0)
    assert lock["invalid_fields"] == ["maximum-lock-lift"]
    # The nulled sentinel is kept as an explicit null inside the lock record.
    assert "maximum-lock-lift" in lock and lock["maximum-lock-lift"] is None
    assert ("location" in lock) is (detail == "full")


@pytest.mark.parametrize("detail", ["summary", "full"])
@pytest.mark.parametrize("scenario", LOCK_SCENARIOS)
def test_cli_mcp_parity_for_locks(configured, scenario: str, detail: str) -> None:
    location = SITE_LOCATION if scenario == "not_applicable" else WEBB_LOCATION
    include = scenario != "omitted"
    outputs = []
    for surface in ("mcp", "cli"):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            _arm_describe(mocked, location)
            _arm_lock_scenario(mocked, scenario)
            if surface == "mcp":
                args = {"office": "SWT", "name": location["name"], "detail": detail}
                if include:
                    args["include_locks"] = True
                outputs.append(_mcp_describe(args))
            else:
                extra = ["--detail", detail] + (["--include-locks"] if include else [])
                outputs.append(_cli_describe(f"SWT/{location['name']}", *extra))
        get_cache().clear()
    mcp_out, cli_out = outputs
    for out in outputs:
        _assert_lock_scenario(out, scenario, detail)
    # #143 is fixed: both surfaces serialize through the same model, so compare
    # the whole payload exactly, with no null normalization.
    assert mcp_out == cli_out


@pytest.mark.parametrize(
    ("which", "body"),
    [
        ("list", "<html>maintenance</html>"),
        ("list", {"locks": []}),
        ("detail", "<html>x</html>"),
        ("detail", {"lock-width": 1.0}),
        ("list", [{}]),
        ("list", [{"location": {"name": 42}}]),
        ("list", [LOCKS_LIST[0], {"location": {}}]),
        # A blank lock name would be handed to `get_lock`, whose own guard raises
        # a raw ValueError that escapes the error envelope; reject it up front.
        ("list", [{"location": {"name": ""}}]),
        ("list", [{"location": {"name": "   "}}]),
        ("detail", {**LOCK_EN, "location": {**LOCK_EN["location"], "name": ""}}),
    ],
)
def test_malformed_lock_response_fails_and_is_not_cached(
    configured, mocked, which: str, body: Any
) -> None:
    """A 200 whose body isn't the expected shape (e.g. an HTML error page, which
    cwms-python returns as a string) must report failure, not an empty `ok`,
    and must not be cached."""
    kwargs: dict[str, Any] = (
        {"body": body, "content_type": "text/html"} if isinstance(body, str) else {"json": body}
    )
    if which == "list":
        mocked.add(responses.GET, f"{API_ROOT}projects/locks", status=200, **kwargs)
    else:
        _arm_lock_list(mocked)
        mocked.add(responses.GET, f"{API_ROOT}projects/locks/WEBB-Lock", status=200, **kwargs)
    out = locks.lookup("SWT", "WEBB", "PROJECT")
    assert out["lock_lookup_status"] == "failed"
    before = len(mocked.calls)
    locks.lookup("SWT", "WEBB", "PROJECT")
    assert len(mocked.calls) > before, "a malformed response must not be cached"


def test_describe_with_blank_lock_name_degrades_instead_of_raising(configured, mocked) -> None:
    _arm_describe(mocked, WEBB_LOCATION)
    _arm_lock_list(mocked, body=[{"location": {"name": ""}}])
    out = places.describe_place("SWT", "WEBB", include_locks=True)
    assert out["lock_lookup_status"] == "failed"
    assert out["locks"] == []
    assert out["partial"] is True
    assert "lock_lookup_failed" in out["partial_reasons"]


def test_upstream_status_prefers_a_status_bearing_failure(configured, mocked) -> None:
    """A transport error on the first lock must not hide a later lock's HTTP 500."""
    import requests

    second = json.loads(json.dumps(LOCKS_LIST[0]))
    second["location"]["name"] = "WEBB-Lock2"
    _arm_lock_list(mocked, body=[LOCKS_LIST[0], second])
    mocked.add(
        responses.GET,
        f"{API_ROOT}projects/locks/WEBB-Lock",
        body=requests.exceptions.ConnectionError("refused"),
    )
    _arm_lock_detail(mocked, name="WEBB-Lock2", status=500)
    out = locks.lookup("SWT", "WEBB", "PROJECT")
    assert out["lock_lookup_status"] == "failed"
    assert out["lock_upstream_status"] == 500
