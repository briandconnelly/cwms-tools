"""Empty bodies on list-shaped endpoints (#152).

cwms-python 1.1.2+ returns `{}` for an empty 200/204 body. On the catalog,
levels, and timeseries endpoints a real empty result still carries its
collection key (live CDA, 2026-10-11: `{"entries": []}`, `{"levels": []}`,
`{"values": [], ...}`), so a body without one must raise a temporary
upstream_error and never reach the cache. Driven through the real client with
mocked HTTP; every negative has a known-positive control.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import cwms
import pytest
import responses

from cwms_tools.core import catalog, levels, session, timeseries
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode
from cwms_tools.core.upstream import upstream_call

API_ROOT = "https://example.test/cwms-data/"

EMPTY_BODIES = pytest.mark.parametrize("status", [200, 204], ids=["empty-200", "204"])

LEVEL_ID = "FOSS.Elev.Inst.0.Top of Conservation"
TS_ID = "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"

# Real empty results, as live CDA returns them.
EMPTY_CATALOG = {"entries": [], "page-size": 500, "total": 0}
EMPTY_LEVELS = {"levels": [], "page-size": 100, "total": 0}
EMPTY_WINDOW = {
    "name": TS_ID,
    "office-id": "SWT",
    "units": "ft",
    "value-columns": [{"name": "date-time", "ordinal": 1}, {"name": "value", "ordinal": 2}],
    "values": [],
    "total": 0,
}


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", API_ROOT)
    monkeypatch.delenv("CWMS_TOOLS_OPERATOR_EMAIL", raising=False)
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


def _calls_to(mocked, path: str) -> int:
    return sum(1 for c in mocked.calls if c.request.url.split("?")[0] == f"{API_ROOT}{path}")


def _assert_empty_collection(exc_info, *, endpoint: str, field: str, value: str) -> None:
    env = exc_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "empty or unrecognized collection body" in env.message
    assert env.details is not None
    assert (env.details.field, env.details.value) == (field, value)
    assert env.source.endpoints_called == [endpoint]


# --------------------------------------------------------------------------
# Catalogs
# --------------------------------------------------------------------------

CATALOGS = pytest.mark.parametrize(
    ("fetch", "path"),
    [
        (catalog.get_locations_catalog, "catalog/LOCATIONS"),
        (catalog.get_timeseries_catalog, "catalog/TIMESERIES"),
    ],
    ids=["locations", "timeseries"],
)


@CATALOGS
@EMPTY_BODIES
def test_catalog_empty_body_raises_and_is_not_cached(
    configured, mocked, fetch, path, status
) -> None:
    url = f"{API_ROOT}{path}"
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, json=EMPTY_CATALOG, status=200)

    for _ in range(2):
        with pytest.raises(CwmsToolsError) as ex_info:
            fetch("SWT")
        _assert_empty_collection(ex_info, endpoint=f"/{path}", field="office_id", value="SWT")

    # Control: a real *empty* catalog is accepted and cached.
    assert fetch("SWT") == EMPTY_CATALOG
    assert fetch("SWT") == EMPTY_CATALOG
    assert _calls_to(mocked, path) == 3


def test_locations_catalog_accepts_nested_shape(configured, mocked) -> None:
    """The emptiness check is the parser's verdict, so a shape the parser reads
    but no flat key list would (`locations.location`) is still accepted and cached."""
    payload = {"locations": {"location": [{"name": "FOSS", "office-id": "SWT"}]}}
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=payload)
    assert catalog.get_locations_catalog("SWT") == payload
    assert catalog.get_locations_catalog("SWT") == payload
    assert _calls_to(mocked, "catalog/LOCATIONS") == 1
    assert [r["name"] for r in catalog._iter_location_entries(payload)] == ["FOSS"]


def test_catalog_parsers_distinguish_empty_from_unrecognized() -> None:
    # A bare list can't come through cwms-python's pager, but the parsers keep
    # accepting it for callers that hand them one directly.
    rows = [{"name": "FOSS"}]
    assert catalog._location_entries(rows) == rows
    assert catalog._ts_entries(rows) == rows
    for parse in (catalog._location_entries, catalog._ts_entries):
        assert parse({"entries": []}) == []
        assert parse({}) is None
        assert parse("<html>") is None


@CATALOGS
@EMPTY_BODIES
def test_catalog_empty_later_page_raises_temporary(configured, mocked, fetch, path, status) -> None:
    """An empty body on page 2+ makes cwms-python index `{}` by its selector;
    the KeyError must surface as an envelope, not a raw exception."""
    url = f"{API_ROOT}{path}"
    mocked.add(responses.GET, url, json={"entries": [{"name": "FOSS"}], "next-page": "p2"})
    mocked.add(responses.GET, url, body="", status=status)
    with pytest.raises(CwmsToolsError) as ex_info:
        fetch("SWT")
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "empty page" in env.message
    assert env.source.endpoints_called == [f"/{path}"]
    assert _calls_to(mocked, path) == 2


def test_catalog_pages_merge_when_every_page_is_real(configured, mocked) -> None:
    """Control for the paging test: two real pages merge and cache."""
    url = f"{API_ROOT}catalog/LOCATIONS"
    mocked.add(responses.GET, url, json={"entries": [{"name": "A"}], "next-page": "p2"})
    mocked.add(responses.GET, url, json={"entries": [{"name": "B"}]})
    payload = catalog.get_locations_catalog("SWT")
    assert [r["name"] for r in payload["entries"]] == ["A", "B"]


@pytest.mark.parametrize("page_selector", [None, "entries"], ids=["unpaged", "paged"])
def test_upstream_call_only_translates_the_page_selector_key_error(page_selector) -> None:
    with pytest.raises(KeyError), upstream_call("/x", operation="op", page_selector=page_selector):
        raise KeyError("ours")
    if page_selector is not None:
        with (
            pytest.raises(CwmsToolsError),
            upstream_call("/x", operation="op", page_selector=page_selector),
        ):
            raise KeyError(page_selector)


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------


@EMPTY_BODIES
def test_list_levels_empty_body_raises_and_is_not_cached(configured, mocked, status) -> None:
    url = f"{API_ROOT}levels"
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, json=EMPTY_LEVELS, status=200)

    for _ in range(2):
        with pytest.raises(CwmsToolsError) as ex_info:
            levels.list_levels("SWT", location="FOSS", parameter="Elev")
        _assert_empty_collection(ex_info, endpoint="/levels", field="office_id", value="SWT")

    # Control: a real empty listing is accepted and cached.
    assert levels.list_levels("SWT", location="FOSS", parameter="Elev") == []
    assert levels.list_levels("SWT", location="FOSS", parameter="Elev") == []
    assert _calls_to(mocked, "levels") == 3


def test_empty_level_body_is_not_a_level(configured, mocked) -> None:
    """Before #152 `{}` became one bogus `{}` row; now there is no applicable level
    to resolve, and the failure is visible."""
    mocked.add(responses.GET, f"{API_ROOT}levels", body="", status=200)
    with pytest.raises(CwmsToolsError):
        levels.resolve_applicable_level(
            "SWT", location="FOSS", parameter="Elev", at=datetime(2026, 5, 17, tzinfo=UTC)
        )


@pytest.mark.parametrize("id_key", ["location-level-id", "level-id", "locationLevelId"])
def test_list_levels_accepts_a_bare_level_dict(configured, mocked, id_key) -> None:
    row = {id_key: LEVEL_ID, "level-date": "2026-01-01T00:00:00Z"}
    mocked.add(responses.GET, f"{API_ROOT}levels", json=row)
    assert levels.list_levels("SWT", location="FOSS", parameter="Elev") == [row]
    assert levels.list_levels("SWT", location="FOSS", parameter="Elev") == [row]
    assert _calls_to(mocked, "levels") == 1


def test_list_levels_rejects_a_dict_without_rows_or_level_id(configured, mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}levels", json={"page-size": 100, "total": 0})
    with pytest.raises(CwmsToolsError) as ex_info:
        levels.list_levels("SWT", location="FOSS", parameter="Elev")
    _assert_empty_collection(ex_info, endpoint="/levels", field="office_id", value="SWT")


LEVEL_CONFIG_URL = re.compile(rf"{re.escape(API_ROOT)}levels/[^/?]+(\?.*)?$")
LEVEL_TS_URL = re.compile(rf"{re.escape(API_ROOT)}levels/[^/?]+/timeseries(\?.*)?$")
CONSTANT_CONFIG = {
    "location-level-id": LEVEL_ID,
    "office-id": "SWT",
    "level-units-id": "ft",
    "level-date": "2000-01-01T06:00:00Z",
    "constant-value": 1652.0,
}
SEASONAL_CONFIG = {
    "location-level-id": LEVEL_ID,
    "office-id": "SWT",
    "level-date": "2026-01-01T00:00:00Z",
    "seasonal-values": [{"value": 1648.0, "offset-months": 0}],
    "interval-months": 12,
}


@EMPTY_BODIES
def test_fetch_level_value_empty_body_raises(configured, mocked, status) -> None:
    mocked.add(responses.GET, LEVEL_CONFIG_URL, body="", status=status)
    mocked.add(responses.GET, LEVEL_CONFIG_URL, json=CONSTANT_CONFIG)
    at = datetime(2026, 5, 17, tzinfo=UTC)

    with pytest.raises(CwmsToolsError) as ex_info:
        levels.fetch_level_value(LEVEL_ID, office="SWT", effective_date=at)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "empty or non-object body" in env.message
    assert env.details is not None
    assert (env.details.field, env.details.value) == ("level_id", LEVEL_ID)

    # Control: a real constant level resolves.
    result = levels.fetch_level_value(LEVEL_ID, office="SWT", effective_date=at)
    assert (result["variety"], result["value"]) == ("constant", 1652.0)


@EMPTY_BODIES
def test_seasonal_workaround_empty_body_raises(configured, mocked, status) -> None:
    mocked.add(responses.GET, LEVEL_CONFIG_URL, json=SEASONAL_CONFIG)
    mocked.add(responses.GET, LEVEL_TS_URL, body="", status=status)
    mocked.add(responses.GET, LEVEL_TS_URL, json={"units": "ft", "values": []})
    at = datetime(2026, 5, 17, tzinfo=UTC)

    with pytest.raises(CwmsToolsError) as ex_info:
        levels.fetch_level_value(LEVEL_ID, office="SWT", effective_date=at)
    _assert_empty_collection(
        ex_info, endpoint=f"/levels/{LEVEL_ID}/timeseries", field="level_id", value=LEVEL_ID
    )
    assert ex_info.value.envelope.source.workaround == "issue-286"

    # Control: a real empty window is still a value-less seasonal result.
    result = levels.fetch_level_value(LEVEL_ID, office="SWT", effective_date=at)
    assert (result["variety"], result["value"]) == ("seasonal", None)
    assert result["source_workaround"] == "issue-286"


# --------------------------------------------------------------------------
# Timeseries
# --------------------------------------------------------------------------

END = datetime(2026, 5, 17, tzinfo=UTC)
BEGIN = END - timedelta(hours=24)


@EMPTY_BODIES
def test_fetch_window_empty_body_raises(configured, mocked, status) -> None:
    url = f"{API_ROOT}timeseries"
    mocked.add(responses.GET, url, body="", status=status)
    mocked.add(responses.GET, url, json=EMPTY_WINDOW)

    with pytest.raises(CwmsToolsError) as ex_info:
        timeseries.fetch_window(TS_ID, office="SWT", begin=BEGIN, end=END)
    _assert_empty_collection(ex_info, endpoint="/timeseries", field="ts_id", value=TS_ID)

    # Control: a real empty window is an empty observation window, not an error.
    result = timeseries.fetch_window(TS_ID, office="SWT", begin=BEGIN, end=END)
    assert result["values"] == []
    assert result["truncated"] is False


@EMPTY_BODIES
def test_fetch_window_empty_later_page_raises_temporary(configured, mocked, status) -> None:
    url = f"{API_ROOT}timeseries"
    mocked.add(
        responses.GET,
        url,
        json={**EMPTY_WINDOW, "values": [[1747440000000, 1.0, 0]], "next-page": "p2"},
    )
    mocked.add(responses.GET, url, body="", status=status)
    with pytest.raises(CwmsToolsError) as ex_info:
        timeseries.fetch_window(TS_ID, office="SWT", begin=BEGIN, end=END)
    env = ex_info.value.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "empty page" in env.message
