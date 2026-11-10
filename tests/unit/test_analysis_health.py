"""Готовность анализа: оценка состояния, подсчёт воркеров очереди и выбор проверки."""

from typing import Any

import pytest

from app.core.config import get_settings
from app.domain.analysis_health import AnalysisHealth, AnalysisHealthStatus, QueueProbe
from app.infrastructure.ml_pipeline import factory
from app.infrastructure.queue import celery_analysis_queue as queue_module
from app.infrastructure.queue.celery_analysis_queue import (
    CeleryAnalysisQueue,
    analysis_queue_name,
    count_consumers,
)


def _health(healthy: bool | None, consumers: int | None) -> AnalysisHealth:
    return AnalysisHealth(
        provider="ml_http",
        contract_version="2.0",
        pipeline_healthy=healthy,
        queue=QueueProbe(queue="analysis_v2", consumers=consumers),
    )


@pytest.mark.parametrize(
    ("healthy", "consumers", "status"),
    [
        (True, 2, AnalysisHealthStatus.OK),
        (False, 2, AnalysisHealthStatus.DOWN),
        (True, 0, AnalysisHealthStatus.DOWN),
        (False, None, AnalysisHealthStatus.DOWN),
        (None, 0, AnalysisHealthStatus.DOWN),
        (None, 1, AnalysisHealthStatus.UNKNOWN),
        (True, None, AnalysisHealthStatus.UNKNOWN),
    ],
)
def test_status(healthy: bool | None, consumers: int | None, status: AnalysisHealthStatus) -> None:
    # «Точно не работает» важнее «не удалось проверить»: down перекрывает unknown.
    assert _health(healthy, consumers).status is status


def test_count_consumers_counts_workers_not_queues() -> None:
    reply = {
        "default@a": [{"name": "celery"}],
        "analysis@a": [{"name": "analysis_v2"}, {"name": "celery"}],
        "analysis@b": [{"name": "analysis_v2"}],
        "odd@c": [{}],
    }
    assert count_consumers(reply, "analysis_v2") == 2
    assert count_consumers(reply, "celery") == 2
    assert count_consumers({}, "analysis_v2") == 0


def _settings(**update: Any) -> Any:
    return get_settings().model_copy(update=update)


def test_queue_name_follows_provider() -> None:
    assert analysis_queue_name(_settings(analysis_provider="legacy_llm")) == "celery"
    assert (
        analysis_queue_name(_settings(analysis_provider="ml_http", analysis_v2_queue="ml")) == "ml"
    )


class _Inspect:
    def __init__(self, reply: object = None, error: Exception | None = None) -> None:
        self.reply, self.error = reply, error

    def active_queues(self) -> object:
        if self.error:
            raise self.error
        return self.reply


def _patch_inspect(monkeypatch: pytest.MonkeyPatch, inspect: _Inspect) -> None:
    monkeypatch.setattr(
        queue_module.celery_app.control, "inspect", lambda timeout: inspect, raising=False
    )
    monkeypatch.setattr(queue_module, "analysis_queue_name", lambda: "analysis_v2")


async def test_probe_counts_listening_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_inspect(monkeypatch, _Inspect({"analysis@a": [{"name": "analysis_v2"}]}))
    assert await CeleryAnalysisQueue().probe() == QueueProbe("analysis_v2", 1)


async def test_probe_without_replies_means_no_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Celery возвращает None, если за таймаут не ответил ни один воркер.
    _patch_inspect(monkeypatch, _Inspect(None))
    assert await CeleryAnalysisQueue().probe() == QueueProbe("analysis_v2", 0)


async def test_probe_with_broken_broker_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_inspect(monkeypatch, _Inspect(error=ConnectionError("redis down")))
    assert await CeleryAnalysisQueue().probe() == QueueProbe("analysis_v2", None)


async def test_pipeline_health_check_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await factory.pipeline_health_check(_settings(analysis_provider="stub"))() is True
    # ml_plugin установлен только в образ воркера: из API проверить нечем.
    plugin = _settings(analysis_provider="ml_plugin", ml_plugin_entrypoint="missing.pkg:run")
    assert await factory.pipeline_health_check(plugin)() is None
    http = _settings(analysis_provider="ml_http", ml_endpoint="http://ml.test/v1/analyze")
    assert await factory.pipeline_health_check(http)() is None

    class _Llm:
        async def health_check(self) -> bool:
            return False

    monkeypatch.setattr(factory, "get_llm_client", lambda settings: _Llm())
    assert await factory.pipeline_health_check(_settings(analysis_provider="legacy_llm"))() is False


def test_contract_version_only_for_contract_providers() -> None:
    assert factory.contract_version(_settings(analysis_provider="legacy_llm")) is None
    assert factory.contract_version(_settings(analysis_provider="stub")) == "2.0"
