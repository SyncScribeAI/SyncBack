"""Маршрутизация задачи анализа v2 в отдельную очередь и согласованность с запуском воркеров."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.workers.celery_app import DEFAULT_QUEUE, celery_app

_ROOT = Path(__file__).resolve().parents[2]


def _queue_of(task_name: str) -> str:
    route = celery_app.amqp.router.route({}, task_name, (), {})
    return route["queue"].name


def test_v2_task_goes_to_analysis_queue() -> None:
    assert _queue_of("analyze_document_for_job") == get_settings().analysis_v2_queue
    assert get_settings().analysis_v2_queue != DEFAULT_QUEUE


@pytest.mark.parametrize("task_name", ["process_source_for_analysis_job", "finalize_analysis_job"])
def test_legacy_tasks_stay_in_default_queue(task_name: str) -> None:
    assert _queue_of(task_name) == DEFAULT_QUEUE


def test_retry_keeps_queue() -> None:
    # self.retry отправляет задачу заново через apply_async с тем же именем: маршрут по имени
    # возвращает её в ту же очередь, а не в общую.
    signature = celery_app.tasks["analyze_document_for_job"].signature(
        kwargs={"job_id": "j", "source_ids": [], "attempt": 1}
    )
    assert _queue_of(signature.task) == get_settings().analysis_v2_queue


@pytest.mark.parametrize("bad", ["", "bad queue", "a" * 65, "q;rm"])
def test_queue_name_is_validated(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("ANALYSIS_V2_QUEUE", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_default_queue_is_allowed_for_single_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_V2_QUEUE", DEFAULT_QUEUE)
    assert Settings().analysis_v2_queue == DEFAULT_QUEUE


def test_compose_worker_listens_to_same_queue_by_default() -> None:
    # Значение по умолчанию в compose и в настройках должно совпадать: иначе при пустом
    # ANALYSIS_V2_QUEUE задачи уходили бы в очередь, которую никто не слушает.
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service = compose.split("\n  worker-analysis:\n", 1)[1]
    match = re.search(r"- -Q\n\s+- \$\{ANALYSIS_V2_QUEUE:-([^}]+)\}", service)
    assert match is not None
    assert match.group(1) == Settings.model_fields["analysis_v2_queue"].default
    assert "--concurrency=${ANALYSIS_V2_CONCURRENCY:-1}" in service


def test_default_worker_listens_only_to_default_queue() -> None:
    dockerfile = (_ROOT / "docker" / "worker.Dockerfile").read_text(encoding="utf-8")
    assert f'"-Q", "{DEFAULT_QUEUE}"' in dockerfile
