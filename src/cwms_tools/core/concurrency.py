"""Bounded-executor concurrency primitive.

Single `ThreadPoolExecutor` owned by this module — every blocking call into
`cwms-python` flows through `run_sync` (async) or `_EXECUTOR.submit(...).result()`
(sync), so the per-host concurrency ceiling is `CWMS_TOOLS_WORKERS` regardless of
how many concurrent MCP tool calls arrive.

Crucially: **do not** use `asyncio.to_thread(...)` from MCP handlers. That
dispatches onto the event loop's default unbounded thread pool, bypassing this
ceiling and producing the oversubscription pattern described in the plan.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_DEFAULT_WORKERS = 8
T = TypeVar("T")


def _resolve_max_workers() -> int:
    raw = os.environ.get("CWMS_TOOLS_WORKERS")
    if raw is None:
        return _DEFAULT_WORKERS
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_WORKERS
    return max(1, n)


MAX_WORKERS: int = _resolve_max_workers()
_EXECUTOR: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=MAX_WORKERS,
    thread_name_prefix="cwms-tools",
)


def get_executor() -> ThreadPoolExecutor:
    """Return the module-owned bounded executor. Test seams can monkeypatch this."""
    return _EXECUTOR


def submit(fn: Callable[..., T], /, *args: object, **kwargs: object) -> Future[T]:
    """Submit a sync callable to the bounded executor; returns a `concurrent.futures.Future`.

    Use from sync (CLI) callers that want concurrent fan-out without async overhead.
    """
    return _EXECUTOR.submit(fn, *args, **kwargs)


async def run_sync(fn: Callable[..., T], /, *args: object, **kwargs: object) -> T:
    """Run a blocking callable on the bounded executor and `await` the result.

    Equivalent to `await loop.run_in_executor(_EXECUTOR, fn, *args)` but accepts kwargs
    via `functools.partial` so call sites can stay readable.

    Use from `async def` MCP tool handlers — never `asyncio.to_thread`.
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        bound = partial(fn, *args, **kwargs)
        return await loop.run_in_executor(_EXECUTOR, bound)
    return await loop.run_in_executor(_EXECUTOR, fn, *args)


def shutdown(*, wait: bool = True) -> None:
    """Shut down the executor. Called from server / CLI lifecycle hooks."""
    _EXECUTOR.shutdown(wait=wait)


__all__ = [
    "MAX_WORKERS",
    "get_executor",
    "run_sync",
    "shutdown",
    "submit",
]
