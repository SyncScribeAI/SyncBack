"""Логическая отмена: состояние вычисления и снятие шага, который ждёт ML или источники."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import pytest

from app.domain.lifecycle import AnalysisExecutionLifecycle
from app.domain.value_objects import AnalysisExecutionStateVO as State
from app.domain.value_objects import AnalysisJobStatusVO as Status
from app.workers.pipeline.cancellation import AnalysisCancelled, run_unless_cancelled


@pytest.mark.parametrize(
    ("state", "previous", "target", "expected"),
    [
        (State.NOT_STARTED, Status.PENDING, Status.PROCESSING, State.RUNNING),
        (State.RUNNING, Status.PROCESSING, Status.CANCELLED, State.CANCEL_REQUESTED),
        (State.NOT_STARTED, Status.PENDING, Status.CANCELLED, State.STOPPED),
        (State.UNKNOWN, Status.PROCESSING, Status.CANCELLED, State.UNKNOWN),
        (None, Status.PROCESSING, Status.CANCELLED, State.CANCEL_REQUESTED),
        (None, Status.PENDING, Status.CANCELLED, State.STOPPED),
        (State.CANCEL_REQUESTED, Status.CANCELLED, Status.CANCELLED, State.CANCEL_REQUESTED),
        (State.RUNNING, Status.PROCESSING, Status.SUCCESS, State.COMPLETED),
        (State.RUNNING, Status.PROCESSING, Status.FAILED, State.COMPLETED),
        (None, Status.PENDING, Status.PENDING, State.NOT_STARTED),
    ],
)
def test_state_follows_status(
    state: State | None, previous: Status, target: Status, expected: State
) -> None:
    assert AnalysisExecutionLifecycle.after_status(state, previous, target) is expected


def test_abandoned_ml_request_gives_unknown() -> None:
    assert AnalysisExecutionLifecycle.worker_stopped(ml_request_abandoned=True) is State.UNKNOWN
    assert AnalysisExecutionLifecycle.worker_stopped(ml_request_abandoned=False) is State.STOPPED
    assert AnalysisExecutionLifecycle.waiting_retry(ml_request_abandoned=True) is State.UNKNOWN
    assert AnalysisExecutionLifecycle.waiting_retry(ml_request_abandoned=False) is State.NOT_STARTED


def _flag(value: bool = False) -> tuple[list[bool], Callable[[], Awaitable[bool]]]:
    box = [value]

    async def probe() -> bool:
        return box[0]

    return box, probe


async def test_cancel_interrupts_long_ml_wait() -> None:
    box, probe = _flag()
    interrupted = asyncio.Event()

    async def wait_for_ml() -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            interrupted.set()
            raise
        return "ответ"

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        box[0] = True

    started = time.perf_counter()
    canceller = asyncio.create_task(cancel_soon())
    with pytest.raises(AnalysisCancelled) as caught:
        await run_unless_cancelled(wait_for_ml(), probe, 0.01, ml_request=True)
    await canceller
    assert time.perf_counter() - started < 5
    assert interrupted.is_set()
    assert caught.value.ml_request_abandoned is True


async def test_source_step_cancel_does_not_mark_ml_abandoned() -> None:
    _, probe = _flag(True)

    async def fetch() -> None:
        await asyncio.sleep(60)

    with pytest.raises(AnalysisCancelled) as caught:
        await run_unless_cancelled(fetch(), probe, 0.01)
    assert caught.value.ml_request_abandoned is False


async def test_finished_step_returns_result_without_cancel() -> None:
    _, probe = _flag()

    async def quick() -> int:
        await asyncio.sleep(0.03)
        return 7

    assert await run_unless_cancelled(quick(), probe, 0.01) == 7


async def test_probe_failure_does_not_break_step() -> None:
    calls = 0

    async def broken_probe() -> bool:
        nonlocal calls
        calls += 1
        raise ConnectionError("БД недоступна")

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "готово"

    assert await run_unless_cancelled(slow(), broken_probe, 0.01) == "готово"
    assert calls >= 1
