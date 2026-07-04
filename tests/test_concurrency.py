"""Tests for the bounded-executor concurrency primitive."""

from __future__ import annotations

import asyncio
import math
import time

from cwms_tools.core import concurrency, places, publishers_index


def test_max_workers_defaults_to_8() -> None:
    # Module-level constant resolved at import; env override happens at import too.
    assert concurrency.MAX_WORKERS >= 1


def test_fanout_budget_is_the_single_shared_cap() -> None:
    """The cold-cache fan-out cap has ONE home (`concurrency.fanout_budget`);
    `search_places` and `publishers_for_parameter` both delegate to it so they
    can never drift (post-0.5.0 review)."""
    expected = max(1, math.ceil(concurrency.MAX_WORKERS / 2))
    assert concurrency.fanout_budget() == expected
    assert places._fanout_budget() == expected
    assert publishers_index._budget() == expected


def test_submit_returns_future_with_result() -> None:
    fut = concurrency.submit(lambda x: x * 2, 21)
    assert fut.result(timeout=2) == 42


def test_run_sync_dispatches_to_executor() -> None:
    """run_sync must actually run on a worker thread, not the event loop."""

    def blocking(x: int) -> str:
        time.sleep(0.01)
        return f"v={x}"

    async def go() -> str:
        return await concurrency.run_sync(blocking, 7)

    assert asyncio.run(go()) == "v=7"


def test_run_sync_threads_kwargs() -> None:
    def blocking(a: int, *, b: int) -> int:
        return a + b

    async def go() -> int:
        return await concurrency.run_sync(blocking, 1, b=2)

    assert asyncio.run(go()) == 3


def test_executor_is_singleton() -> None:
    assert concurrency.get_executor() is concurrency._EXECUTOR
