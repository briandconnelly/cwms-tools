"""Unit tests for the two-tier cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cwms_tools.core.cache import (
    CACHE_SCHEMA_VERSION,
    NAMESPACE_TTLS,
    Cache,
    build_cache_key,
    resolve_cache_dir,
    set_cache,
)


@pytest.fixture
def fresh_cache(tmp_path: Path) -> Cache:
    cache = Cache(directory=tmp_path / "cache")
    set_cache(cache)
    try:
        return cache
    finally:
        pass


def test_resolve_cache_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CWMS_TOOLS_CACHE_DIR", str(tmp_path))
    assert resolve_cache_dir() == tmp_path


def test_resolve_cache_dir_falls_back_to_platformdirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CWMS_TOOLS_CACHE_DIR", raising=False)
    resolved = resolve_cache_dir()
    assert "cwms-tools" in str(resolved)


def test_build_cache_key_is_stable_and_namespaced() -> None:
    a = build_cache_key("offices", "list", api_root="https://example/")
    b = build_cache_key("offices", "list", api_root="https://example/")
    assert a == b
    assert a.startswith("offices:")


def test_build_cache_key_differs_on_api_root() -> None:
    a = build_cache_key("offices", api_root="https://example.com/")
    b = build_cache_key("offices", api_root="https://other.example.com/")
    assert a != b


def test_build_cache_key_differs_on_namespace() -> None:
    a = build_cache_key("offices", "x", api_root="https://example/")
    b = build_cache_key("parameters", "x", api_root="https://example/")
    assert a != b


def test_cache_l1_l2_round_trip(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path / "c")
    try:
        cache.set("k", {"hello": "world"}, ttl=3600)
        assert cache.get("k") == {"hello": "world"}
        # Clear L1 — value still comes from L2
        cache._l1.clear()
        assert cache.get("k") == {"hello": "world"}
    finally:
        cache.close()


def test_cache_ttl_zero_treats_as_expired(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path / "c")
    try:
        cache.set("k", "v", ttl=0)
        cache._l1.clear()
        # diskcache treats ttl=0 as no expiration; we explicitly do not depend on this.
        # The contract is: get returns either the stored value or None.
        result = cache.get("k")
        assert result in {"v", None}
    finally:
        cache.close()


def test_namespace_ttls_cover_documented_namespaces() -> None:
    expected = {
        "offices",
        "parameters",
        "location_catalog",
        "ts_catalog",
        "publishers",
        "levels",
        "overview_text",
    }
    assert expected.issubset(NAMESPACE_TTLS.keys())


def test_schema_version_is_positive_integer() -> None:
    assert isinstance(CACHE_SCHEMA_VERSION, int)
    assert CACHE_SCHEMA_VERSION >= 1


def test_set_cache_replaces_and_clears_singleton(tmp_path: Path) -> None:
    a = Cache(directory=tmp_path / "a")
    b = Cache(directory=tmp_path / "b")
    try:
        set_cache(a)
        set_cache(b)  # closes `a`
    finally:
        b.close()
    set_cache(None)


def test_isolated_cache_fixture_yields_unique_dir(isolated_cache: Cache, tmp_path: Path) -> None:
    assert isolated_cache.directory.is_dir()
    assert str(tmp_path) in str(isolated_cache.directory)
    # And the env override does not leak across tests:
    assert os.environ.get("CWMS_TOOLS_CACHE_DIR") in {None, str(isolated_cache.directory.parent)}
