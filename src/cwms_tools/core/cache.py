"""Two-tier cache facade: in-process LRU (L1) over a diskcache store (L2).

Disk location resolves via `platformdirs.user_cache_dir`, overridable by
`CWMS_TOOLS_CACHE_DIR`. Cache keys include the cwms-python installed version
and a cwms-tools cache-schema version so library upgrades invalidate cleanly
without manual purges.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import diskcache
from platformdirs import user_cache_dir

if TYPE_CHECKING:
    from collections.abc import Callable

# Bump this when the cached payload schema changes in a backwards-incompatible way.
# v2: ts catalog now requires include_extents=True; enrich_locations dedupes by name.
CACHE_SCHEMA_VERSION: Final[int] = 2

_L1_MAX_ENTRIES: Final[int] = 1024


# Namespace → TTL in seconds. Mirrors the plan's "Caching" table.
NAMESPACE_TTLS: Final[dict[str, int]] = {
    "offices": 7 * 24 * 3600,
    "parameters": 7 * 24 * 3600,
    "location_catalog": 6 * 3600,
    "ts_catalog": 6 * 3600,
    "publishers": 24 * 3600,
    "levels": 24 * 3600,
    "overview_text": 365 * 24 * 3600,  # bundled; effectively never refetched
}


def _cwms_python_version() -> str:
    try:
        return version("cwms-python")
    except PackageNotFoundError:  # pragma: no cover - install-time issue
        return "unknown"


def resolve_cache_dir() -> Path:
    """Resolve the on-disk cache directory, honoring `CWMS_TOOLS_CACHE_DIR`."""
    override = os.environ.get("CWMS_TOOLS_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_cache_dir("cwms-tools", "cwms-tools"))


def _stable_key_part(value: Any) -> str:
    """Hash-friendly canonical form of any JSON-serializable value."""
    return json.dumps(value, sort_keys=True, default=str)


def build_cache_key(
    namespace: str,
    *parts: Any,
    api_root: str,
) -> str:
    """Build a versioned cache key.

    Includes the cwms-tools cache-schema version, the installed cwms-python
    version, and the configured API root so root-switches and dependency bumps
    invalidate cleanly.
    """
    payload = {
        "schema": CACHE_SCHEMA_VERSION,
        "cwms_python": _cwms_python_version(),
        "api_root": api_root,
        "namespace": namespace,
        "parts": [_stable_key_part(p) for p in parts],
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return f"{namespace}:{digest}"


class _LRU:
    """Tiny in-process LRU used as the L1 tier.

    Each entry carries an absolute expiry on the same wall clock diskcache uses
    (`None` = never expires), so a long-running process honors namespace TTLs
    without waiting for a restart or an LRU eviction.
    """

    def __init__(
        self,
        max_entries: int = _L1_MAX_ENTRIES,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expire_time = entry
        # Same boundary as diskcache: an entry is live only while expire_time > now.
        if expire_time is not None and expire_time <= self._clock():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, *, expire_time: float | None) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, expire_time)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


class Cache:
    """Two-tier cache façade. Pass-through `use_cache=False` honored at call sites."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = directory if directory is not None else resolve_cache_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        # `clock` must read the wall clock diskcache stamps expiries with; it is
        # injectable so tests can expire L1 entries without sleeping.
        self._clock = clock
        self._l1 = _LRU(clock=clock)
        self._l2 = diskcache.Cache(str(self._dir))

    @property
    def directory(self) -> Path:
        return self._dir

    def get(self, key: str) -> Any | None:
        # Global bypass flag: `--no-cache` / `--isolated` on the CLI.
        if os.environ.get("_CWMS_TOOLS_NO_CACHE") == "1":
            return None
        hit = self._l1.get(key)
        if hit is not None:
            return hit
        # Refill L1 with L2's absolute expiry, not a fresh TTL.
        value, expire_time = self._l2.get(key, default=None, expire_time=True)
        if value is not None:
            self._l1.set(key, value, expire_time=expire_time)
        return value

    def set(self, key: str, value: Any, *, ttl: int | None) -> None:
        expire_time = None if ttl is None else self._clock() + ttl
        self._l1.set(key, value, expire_time=expire_time)
        self._l2.set(key, value, expire=ttl)

    def delete(self, key: str) -> None:
        self._l1.delete(key)
        with suppress(KeyError):
            self._l2.delete(key)

    def clear(self) -> None:
        self._l1.clear()
        self._l2.clear()

    def close(self) -> None:
        self._l2.close()

    def ttl_for(self, namespace: str) -> int | None:
        return NAMESPACE_TTLS.get(namespace)


# Module-level singleton state. Wrapped in a dict to keep the `global` keyword
# out of the helper functions (PLW0603) — the dict reference is constant; only
# its contents change.
_state: dict[str, Cache | None] = {"current": None}


def get_cache() -> Cache:
    """Module-level cache singleton. Tests can replace via `set_cache`."""
    if _state["current"] is None:
        _state["current"] = Cache()
    cache = _state["current"]
    assert cache is not None  # for ty / mypy narrowing
    return cache


def set_cache(cache: Cache | None) -> None:
    """Test seam: substitute a cache (e.g. tmp_path-rooted) or clear the singleton."""
    current = _state["current"]
    if current is not None and cache is not current:
        current.close()
    _state["current"] = cache


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "NAMESPACE_TTLS",
    "Cache",
    "build_cache_key",
    "get_cache",
    "resolve_cache_dir",
    "set_cache",
]
