"""Обёртка Celery задачи анализа v2 и выбор конвейера в очереди."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded

import app.infrastructure.queue.celery_analysis_queue as queue_module
from app.core.config import get_settings
from app.domain.value_objects import AnalysisJobStatusVO
from app.workers.tasks import analysis_v2_tasks as v2

task = v2.analyze_document_for_job
JOB_ID = "11111111-1111-1111-1111-111111111111"
SOURCES = ["22222222-2222-2222-2222-222222222222"]


@pytest.fixture
def finished(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def finish(job_id, status, error_code=None, error_message=None, **_: Any) -> bool:
        calls.append((job_id, status, error_code, error_message))
        return True

    monkeypatch.setattr(v2, "finish", finish)
    return calls


def _attempt_returns(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    async def analyze_document(job_id: str, source_ids: list[str], attempt: int) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(v2, "analyze_document", analyze_document)


def test_task_limits_follow_settings() -> None:
    limit = get_settings().analysis_task_time_limit_seconds
    assert (task.soft_time_limit, task.time_limit, task.max_retries) == (limit, limit + 30, None)


def test_retry_passes_next_attempt_and_countdown(monkeypatch: pytest.MonkeyPatch) -> None:
    _attempt_returns(monkeypatch, v2.RetryLater(countdown=60))
    calls: list[dict[str, Any]] = []

    def retry(**options: Any) -> Retry:
        calls.append(options)
        return Retry("retry")

    monkeypatch.setattr(task, "retry", retry)

    with pytest.raises(Retry):
        task(JOB_ID, SOURCES, attempt=1)

    assert calls == [
        {
            "args": (),
            "kwargs": {"job_id": JOB_ID, "source_ids": SOURCES, "attempt": 2},
            "countdown": 60,
        }
    ]


def test_completed_attempt_does_not_retry(monkeypatch: pytest.MonkeyPatch, finished) -> None:
    _attempt_returns(monkeypatch, None)
    monkeypatch.setattr(task, "retry", lambda **_: pytest.fail("повтор не ожидался"))

    assert task(JOB_ID, SOURCES) is None
    assert finished == []


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (SoftTimeLimitExceeded(), "LLM_UNAVAILABLE", "Анализ не уложился в 1800 с"),
        (RuntimeError("ошибка в коде"), "GENERATION_ERROR", "Внутренняя ошибка анализа"),
    ],
)
def test_unfinished_attempt_marks_job_failed(
    monkeypatch: pytest.MonkeyPatch,
    finished: list[tuple[Any, ...]],
    error: BaseException,
    code: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        v2,
        "_settings",
        get_settings().model_copy(update={"analysis_task_time_limit_seconds": 1800}),
    )
    _attempt_returns(monkeypatch, error)

    assert task(JOB_ID, SOURCES) is None

    assert finished == [(uuid.UUID(JOB_ID), AnalysisJobStatusVO.FAILED, code, message)]


def test_failure_to_record_error_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _attempt_returns(monkeypatch, RuntimeError("БД недоступна"))

    async def finish(*_: Any, **__: Any) -> bool:
        raise ConnectionError("БД недоступна")

    monkeypatch.setattr(v2, "finish", finish)

    assert task(JOB_ID, SOURCES) is None
    assert "не удалось записать ошибку задачи" in caplog.text


@pytest.mark.parametrize(("v2_enabled", "expected"), [(True, "v2-task"), (False, "chord-task")])
def test_queue_routes_by_provider(
    monkeypatch: pytest.MonkeyPatch, v2_enabled: bool, expected: str
) -> None:
    sent: list[tuple[str, tuple[Any, ...]]] = []

    def delay(*args: Any) -> SimpleNamespace:
        sent.append(("v2", args))
        return SimpleNamespace(id="v2-task")

    def chord(header: list[Any]) -> Any:
        sent.append(("chord", tuple(header)))
        return lambda callback: SimpleNamespace(id="chord-task")

    monkeypatch.setattr(queue_module, "uses_document_analyzer", lambda: v2_enabled)
    monkeypatch.setattr(queue_module.analyze_document_for_job, "delay", delay)
    monkeypatch.setattr(queue_module, "chord", chord)

    assert queue_module.CeleryAnalysisQueue._send(JOB_ID, SOURCES) == expected
    assert [kind for kind, _ in sent] == (["v2"] if v2_enabled else ["chord"])
    if v2_enabled:
        assert sent[0][1] == (JOB_ID, SOURCES)
