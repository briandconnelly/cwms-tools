"""Shared pytest fixtures.

The fixture matrix described in the plan lands as placeholders here so
M3-M6 milestones can fill in real recorded responses alongside each tool.
Live-CDA tests are marked `integration` and skipped unless `--run-integration`
(or `CWMS_TOOLS_RUN_INTEGRATION=1`) is given.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import responses

from cwms_tools.cli import render
from cwms_tools.core import offices
from cwms_tools.core.cache import Cache, set_cache

FIXTURES = Path(__file__).parent / "fixtures"

#: Env-var alternative to `--run-integration`, for runners that can't pass flags.
RUN_INTEGRATION_ENV = "CWMS_TOOLS_RUN_INTEGRATION"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help=f"Run `integration`-marked live-CDA tests (or set {RUN_INTEGRATION_ENV}=1).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `integration`-marked tests unless explicitly opted in (#138).

    They hit the live CDA, so they are slow, network-dependent, and must never
    gate an ordinary `uv run pytest`. `-m integration` alone only *selects*
    them; it does not opt in.
    """
    if config.getoption("--run-integration") or os.environ.get(RUN_INTEGRATION_ENV) == "1":
        return
    skip = pytest.mark.skip(
        reason=f"live-CDA test; pass --run-integration or set {RUN_INTEGRATION_ENV}=1"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _clear_cli_env_isolation() -> Iterator[None]:
    """Run before & after every test: scrub leaked `--isolated` / `--no-cache`
    process-env markers and reset cli.render global state. Without this,
    a single `cwms-tools --isolated` invocation in one test would leak the
    env var into every subsequent test in the run."""
    for key in ("_CWMS_TOOLS_ISOLATED", "_CWMS_TOOLS_NO_CACHE"):
        os.environ.pop(key, None)
    render._state.update(machine=False, isolated=False, no_cache=False)
    try:
        yield
    finally:
        for key in ("_CWMS_TOOLS_ISOLATED", "_CWMS_TOOLS_NO_CACHE"):
            os.environ.pop(key, None)
        render._state.update(machine=False, isolated=False, no_cache=False)


@pytest.fixture
def isolated_cache(tmp_path: Path) -> Iterator[Cache]:
    """Provide a tmp_path-rooted cache and install it as the singleton for the test."""
    cache = Cache(directory=tmp_path / "cache")
    set_cache(cache)
    try:
        yield cache
    finally:
        cache.close()
        set_cache(None)


@pytest.fixture
def mocked_cda() -> Iterator[responses.RequestsMock]:
    """Activate `responses` for the test; mocks `cwms-python`'s `requests` traffic.

    Use `mocked_cda.add(...)` inside the test to register expected calls.
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rmock:
        yield rmock


def fixture_path(name: str) -> Path:
    """Resolve a fixture file by name. Tests that need its content `read_text()` it."""
    return FIXTURES / name


@pytest.fixture(autouse=True)
def _offline_office_directory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Office-code validation (#163) is opt-in for unit tests.

    By default the directory lookup reports the degraded fallback, so
    `require_known_office` passes codes through and no test gains a network
    dependency. Tests that exercise validation request `validating_directory`,
    which keeps the real lookup and a clean cooldown map.
    """
    offices._reset_directory_state()
    if "validating_directory" not in request.fixturenames:
        monkeypatch.setattr(offices, "directory_for_validation", lambda: ([], True))
    yield
    offices._reset_directory_state()


@pytest.fixture
def validating_directory() -> None:
    """Marker fixture: run this test with real office-code validation."""
    return None
