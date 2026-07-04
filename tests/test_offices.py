"""Tests for office discovery (`core.offices`) backing the `cwms://offices` resource."""

from __future__ import annotations

import cwms
import pytest
import responses

from cwms_tools.core import offices, session
from cwms_tools.core.cache import Cache, set_cache

API_ROOT = "https://example.test/cwms-data/"

_LIVE_SHAPE = [
    {"name": "NWO", "long-name": "Omaha District", "type": "DIS", "reports-to": "NWDM"},
    {"name": "NWDM", "long-name": "Missouri River Region", "type": "MSCR", "reports-to": "NWD"},
    {"name": "HQ", "long-name": "Headquarters", "type": "HQ", "reports-to": "HQ"},
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


def test_list_offices_parses_and_normalizes(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        records, used_fallback = offices.list_offices()

    assert used_fallback is False
    # Sorted by name.
    assert [r["name"] for r in records] == ["HQ", "NWDM", "NWO"]
    nwo = next(r for r in records if r["name"] == "NWO")
    assert nwo["long_name"] == "Omaha District"
    assert nwo["type"] == "DIS"
    assert nwo["type_label"] == "district"
    assert nwo["reports_to"] == "NWDM"


def test_list_offices_caches_after_first_fetch(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        first, _ = offices.list_offices()
    # Second call must not hit upstream (no mock active → would raise).
    second, used_fallback = offices.list_offices()
    assert second == first
    assert used_fallback is False


def test_list_offices_falls_back_when_upstream_fails(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        records, used_fallback = offices.list_offices()

    assert used_fallback is True
    names = {r["name"] for r in records}
    assert {"NWDM", "NWDP", "SWT"} <= names
    # Fallback records are name-only (no upstream metadata).
    assert all(set(r) == {"name"} for r in records)


def test_list_offices_degrades_gracefully_when_cache_read_fails(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#78: a broken cache (unwritable dir, corrupted store) must not turn a
    working upstream fetch into an unstructured crash — the cwms_list_offices
    tool, the cwms://offices resource, and the CLI offices command all rely
    on this path never raising."""
    from cwms_tools.core.cache import get_cache

    def boom(*_args, **_kwargs):
        raise OSError("cache read exploded")

    monkeypatch.setattr(get_cache(), "get", boom)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        records, used_fallback = offices.list_offices()

    assert used_fallback is False
    assert [r["name"] for r in records] == ["HQ", "NWDM", "NWO"]


def test_list_offices_degrades_gracefully_when_cache_write_fails(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#78: a cache-write failure (e.g. disk full) must not fail an otherwise
    successful fetch."""
    from cwms_tools.core.cache import get_cache

    def boom(*_args, **_kwargs):
        raise OSError("cache write exploded")

    monkeypatch.setattr(get_cache(), "set", boom)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        records, used_fallback = offices.list_offices()

    assert used_fallback is False
    assert [r["name"] for r in records] == ["HQ", "NWDM", "NWO"]


def test_list_offices_degrades_gracefully_when_get_cache_itself_fails(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#78: `get_cache()` can raise too (e.g. cache-dir creation failure on a
    cold start) — must fall through to the upstream fetch, not crash."""
    import cwms_tools.core.offices as offices_module

    def boom():
        raise OSError("cache dir creation exploded")

    monkeypatch.setattr(offices_module, "get_cache", boom)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        records, used_fallback = offices.list_offices()

    assert used_fallback is False
    assert [r["name"] for r in records] == ["HQ", "NWDM", "NWO"]


def test_list_offices_falls_back_on_empty_payload(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=[], status=200)
        _, used_fallback = offices.list_offices()
    assert used_fallback is True


def test_unknown_type_code_passes_through_as_label(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}offices",
            json=[{"name": "ZZZ", "type": "NEWCODE"}],
            status=200,
        )
        records, _ = offices.list_offices()
    assert records[0]["type"] == "NEWCODE"
    assert records[0]["type_label"] == "NEWCODE"


def test_list_office_ids_derives_from_records(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        ids, used_fallback = offices.list_office_ids()
    assert ids == ["HQ", "NWDM", "NWO"]
    assert used_fallback is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Nested under a wrapper key.
        ({"offices": [{"name": "SWT"}]}, ["SWT"]),
        ({"entries": [{"name": "MVS"}]}, ["MVS"]),
        # Bare string items.
        (["SWT", "MVS"], ["SWT", "MVS"]),
        # Alternate id keys.
        ([{"office-id": "NWDM"}], ["NWDM"]),
        # Junk items are skipped; nameless dicts dropped.
        ([1, None, {"type": "DIS"}, {"name": "SWL"}], ["SWL"]),
        # Unrecognized top-level shape.
        ("nope", []),
    ],
)
def test_parse_office_records_tolerates_shapes(raw, expected) -> None:
    records = offices._parse_office_records(raw)
    assert [r["name"] for r in records] == expected


def test_cached_offices_for_locations(configured) -> None:
    from cwms_tools.core.cache import build_cache_key, get_cache

    cache = get_cache()
    cfg = session.current_config()
    cache.set(build_cache_key("location_catalog", "SWT", "", api_root=cfg.api_root), {}, ttl=None)
    assert offices.cached_offices_for_locations() == ["SWT"]


def test_offices_payload_shape_and_guidance(configured) -> None:
    from cwms_tools.mcp.resources import offices_payload

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        payload = offices_payload()

    assert payload["count"] == 3
    assert payload["partial"] is False
    assert [o["name"] for o in payload["offices"]] == ["HQ", "NWDM", "NWO"]
    guidance = payload["guidance"]
    assert guidance["nw_district_stubs"] == ["NWK", "NWO", "NWP", "NWS", "NWW"]
    assert guidance["nw_rollup_targets"]["NWO"] == "NWDM"
    assert guidance["nw_rollup_targets"]["NWP"] == "NWDP"
    assert "NWDM" in guidance["nw_regional_rollup"]


def test_offices_payload_marks_partial_on_degraded_fallback(configured) -> None:
    from cwms_tools.mcp.resources import offices_payload

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        payload = offices_payload()

    assert payload["partial"] is True
    assert payload["count"] > 0


def test_offices_resource_reads_through_async_handler(configured) -> None:
    """The `cwms://offices` resource offloads its blocking cache-miss fetch to
    the bounded executor (`run_sync`); reading it through the async server path
    must still return the payload (regression for the event-loop-block fix)."""
    import asyncio
    import json

    from cwms_tools.mcp.server import build_server

    server = build_server()

    async def go() -> dict:
        result = await server.read_resource("cwms://offices")
        for item in result.contents:
            body = getattr(item, "content", None) or getattr(item, "text", None)
            if body:
                return json.loads(body)
        raise AssertionError("no content")

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        payload = asyncio.run(go())

    assert payload["count"] == 3
    assert {o["name"] for o in payload["offices"]} == {"NWO", "NWDM", "HQ"}


@pytest.mark.parametrize(
    ("office_id", "expected_target"),
    [
        ("NWO", "NWDM"),
        ("NWK", "NWDM"),
        ("NWS", "NWDP"),
        ("NWP", "NWDP"),
        ("NWW", "NWDP"),
    ],
)
def test_nw_rollup_target_maps_each_stub(office_id: str, expected_target: str) -> None:
    assert offices.nw_rollup_target(office_id) == expected_target


def test_nw_rollup_target_falls_back_to_nwdm_for_unknown_office() -> None:
    assert offices.nw_rollup_target("XYZ") == "NWDM"


def test_ghost_office_repair_builds_same_tool_retry() -> None:
    """#69: the repair targets the SAME failing tool with the SAME original
    args, only `office` swapped — never a different tool."""
    repair = offices.ghost_office_repair(
        "NWO", tool="cwms_get_value", args={"name": "FTPK", "parameter": "Elev"}
    )
    assert repair.tool == "cwms_get_value"
    assert repair.arguments == {"name": "FTPK", "parameter": "Elev", "office": "NWDM"}


def test_ghost_office_repair_office_always_overrides_args() -> None:
    """A stray `office` key in `args` must not survive — the swapped target wins."""
    repair = offices.ghost_office_repair("NWS", tool="cwms_browse_region", args={"office": "NWO"})
    assert repair.arguments["office"] == "NWDP"


def test_ghost_office_error_is_the_shared_builder_for_both_surfaces() -> None:
    """`core.locations` and `core.catalog` both build the `ghost_office`
    envelope from this one helper (post-0.5.0 review), so they cannot drift.
    Verify the canonical shape and that both call sites re-export it."""
    from cwms_tools.core import catalog, locations
    from cwms_tools.core.errors import ErrorCode

    err = offices.ghost_office_error("NWO")
    env = err.envelope
    assert env.code is ErrorCode.GHOST_OFFICE
    assert env.details is not None
    assert env.details.field == "office_id"
    assert env.details.value == "NWO"
    assert env.repair is None  # the surface boundary attaches the retry repair
    # Both modules import the shared builder rather than owning a copy.
    assert locations.ghost_office_error is offices.ghost_office_error
    assert catalog.ghost_office_error is offices.ghost_office_error
