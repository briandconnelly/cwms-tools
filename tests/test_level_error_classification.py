"""Level paths classify upstream failures like every other `upstream_call` site (#153).

`list_levels`, `fetch_level_value`, and the issue-286 seasonal bypass used to
catch bare `Exception` and report a non-temporary `upstream_error` (or
`wrapper_bug`) for everything: a 404 was not `not_found`, a 5xx or dropped
connection told the agent not to retry, and our own bugs were reported as
upstream failures.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import cwms
import pytest
import requests
import responses
from cwms.api import ApiError

from cwms_tools.core import levels, session, values
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode

API_ROOT = "https://example.test/cwms-data/"
LEVEL_ID = "FOSS.Elev.Inst.0.Top of Flood"
AT = datetime(2026, 5, 17, tzinfo=UTC)
LEVELS_URL = f"{API_ROOT}levels"
LEVEL_CONFIG_URL = re.compile(rf"{re.escape(API_ROOT)}levels/[^/?]+(\?.*)?$")
LEVEL_TS_URL = re.compile(rf"{re.escape(API_ROOT)}levels/[^/?]+/timeseries(\?.*)?$")
SEASONAL_CONFIG = {
    "location-level-id": LEVEL_ID,
    "office-id": "SWT",
    "level-date": "2026-01-01T00:00:00Z",
    "seasonal-values": [{"value": 1648.0, "offset-months": 0}],
    "interval-months": 12,
}


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


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def _list() -> None:
    levels.list_levels("SWT", level_id_mask="FOSS.Elev.*", use_cache=False)


def _fetch() -> None:
    levels.fetch_level_value(LEVEL_ID, office="SWT", effective_date=AT)


# Each path: (call, url its failing request hits, endpoint reported, setup).
def _no_setup(_mocked) -> None:
    return None


def _seasonal_setup(mocked) -> None:
    mocked.add(responses.GET, LEVEL_CONFIG_URL, json=SEASONAL_CONFIG)


PATHS = {
    "list": (_list, LEVELS_URL, "/levels", _no_setup),
    "fetch": (_fetch, LEVEL_CONFIG_URL, f"/levels/{LEVEL_ID}", _no_setup),
    "seasonal": (_fetch, LEVEL_TS_URL, f"/levels/{LEVEL_ID}/timeseries", _seasonal_setup),
}


def _raise_for(path: str, mocked, **failure) -> CwmsToolsError:
    call, url, _endpoint, setup = PATHS[path]
    setup(mocked)
    mocked.add(responses.GET, url, **failure)
    with pytest.raises(CwmsToolsError) as info:
        call()
    return info.value


def _workaround_for(path: str) -> str | None:
    return "issue-286" if path == "seasonal" else None


@pytest.mark.parametrize("path", PATHS)
def test_404_is_not_found(configured, mocked, path) -> None:
    err = _raise_for(path, mocked, status=404, json={"message": "Location level not found"})
    env = err.envelope
    assert env.code is ErrorCode.NOT_FOUND
    assert env.temporary is False
    assert env.source.endpoints_called == [PATHS[path][2]]
    assert env.source.workaround == _workaround_for(path)


@pytest.mark.parametrize("path", PATHS)
def test_5xx_is_temporary_upstream_error(configured, mocked, path) -> None:
    err = _raise_for(path, mocked, status=500, json={"message": "boom"})
    env = err.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert env.source.endpoints_called == [PATHS[path][2]]
    assert env.source.workaround == _workaround_for(path)


@pytest.mark.parametrize("path", PATHS)
def test_connection_error_is_temporary_upstream_error(configured, mocked, path) -> None:
    exc = requests.exceptions.ConnectionError("connection refused")
    err = _raise_for(path, mocked, body=exc)
    env = err.envelope
    assert env.code is ErrorCode.UPSTREAM_ERROR
    assert env.temporary is True
    assert "ConnectionError" in env.message
    assert env.source.endpoints_called == [PATHS[path][2]]
    assert env.source.workaround == _workaround_for(path)


def _api_error(status: int, headers: dict[str, str] | None = None) -> ApiError:
    response = requests.Response()
    response.status_code = status
    response._content = b"{}"
    response.url = f"{API_ROOT}levels"
    response.headers.update(headers or {})
    return ApiError(response)


def _raiser(exc: BaseException):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


def _seasonal_raiser(exc: BaseException):
    """Fail only the bypass's `/timeseries` request.

    `get_location_level` also goes through `cwms.api.get`, so patching it
    wholesale would fail the config read before the seasonal path is reached.
    """
    real_get = levels.cwms_api.get

    def get(endpoint, *args, **kwargs):
        if endpoint.endswith("/timeseries"):
            raise exc
        return real_get(endpoint, *args, **kwargs)

    return get


# cwms-python retries 429 with backoff, so these patch the wrapper call directly.
@pytest.mark.parametrize(
    ("target", "call"),
    [
        ((levels.ll_api, "get_location_levels"), _list),
        ((levels.ll_api, "get_location_level"), _fetch),
    ],
    ids=["list", "fetch"],
)
def test_429_is_rate_limited_with_retry_after(configured, monkeypatch, target, call) -> None:
    monkeypatch.setattr(*target, _raiser(_api_error(429, {"Retry-After": "7"})))
    with pytest.raises(CwmsToolsError) as info:
        call()
    env = info.value.envelope
    assert env.code is ErrorCode.RATE_LIMITED
    assert env.temporary is True
    assert env.retry_after_ms == 7000


def test_seasonal_429_is_rate_limited(configured, mocked, monkeypatch) -> None:
    mocked.add(responses.GET, LEVEL_CONFIG_URL, json=SEASONAL_CONFIG)
    monkeypatch.setattr(
        levels.cwms_api, "get", _seasonal_raiser(_api_error(429, {"Retry-After": "7"}))
    )
    with pytest.raises(CwmsToolsError) as info:
        _fetch()
    env = info.value.envelope
    assert env.code is ErrorCode.RATE_LIMITED
    assert env.retry_after_ms == 7000
    assert env.source.workaround == "issue-286"


@pytest.mark.parametrize(
    ("target", "replacement", "call"),
    [
        ((levels.ll_api, "get_location_levels"), _raiser, _list),
        ((levels.ll_api, "get_location_level"), _raiser, _fetch),
        ((levels.cwms_api, "get"), _seasonal_raiser, _fetch),
    ],
    ids=["list", "fetch", "seasonal"],
)
def test_programming_errors_propagate_untouched(
    configured, mocked, monkeypatch, target, replacement, call
) -> None:
    mocked.add(responses.GET, LEVEL_CONFIG_URL, json=SEASONAL_CONFIG)
    monkeypatch.setattr(*target, replacement(TypeError("our bug")))
    with pytest.raises(TypeError, match="our bug"):
        call()


# --------------------------------------------------------------------------
# Consumer: `values._resolve_thresholds` still degrades on upstream failures.
# --------------------------------------------------------------------------


def _resolve() -> tuple[list, str]:
    return values._resolve_thresholds(
        office="SWT", location="FOSS", parameter="Elev", observation=1650.0, unit="EN"
    )


def test_thresholds_degrade_when_listing_fails(configured, mocked) -> None:
    mocked.add(responses.GET, LEVELS_URL, status=404, json={"message": "nope"})
    assert _resolve() == ([], "unknown")


def test_thresholds_skip_a_level_whose_value_fetch_fails(configured, mocked) -> None:
    rows = {
        "levels": [
            {
                "location-level-id": LEVEL_ID,
                "specified-level-id": "Top of Flood",
                "level-date": "2026-01-01T00:00:00Z",
            }
        ]
    }
    mocked.add(responses.GET, LEVELS_URL, json=rows)
    mocked.add(responses.GET, LEVEL_CONFIG_URL, status=500, json={"message": "boom"})
    assert _resolve() == ([], "nominal")


def test_thresholds_do_not_mask_programming_errors(configured, monkeypatch) -> None:
    monkeypatch.setattr(levels.ll_api, "get_location_levels", _raiser(TypeError("our bug")))
    with pytest.raises(TypeError, match="our bug"):
        _resolve()
