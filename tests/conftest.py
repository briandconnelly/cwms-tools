"""Shared pytest fixtures.

The fixture matrix described in the plan lands as placeholders here so
M3-M6 milestones can fill in real recorded responses alongside each tool.
Live-CDA tests are marked `integration` and skipped by default.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import responses

from cwms_tools.core.cache import Cache, set_cache

FIXTURES = Path(__file__).parent / "fixtures"


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
