"""Готовность анализа: доступен ли конвейер и слушает ли кто-нибудь очередь задач.

Обе проверки нужны вместе. Живой конвейер без воркера очереди не начнёт ни одной задачи:
они останутся в статусе dispatched. Воркер без конвейера доведёт каждую задачу до
LLM_UNAVAILABLE. «Не удалось проверить» — отдельное состояние: например, у ml_http без
ML_HEALTH_URL или когда брокер не ответил. Выдавать его за «здоров» нельзя.
"""

from dataclasses import dataclass
from enum import StrEnum


class AnalysisHealthStatus(StrEnum):
    OK = "ok"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class QueueProbe:
    queue: str
    # None — брокер не ответил за отведённое время, число воркеров неизвестно.
    consumers: int | None


@dataclass(frozen=True, slots=True)
class AnalysisHealth:
    provider: str
    # None — legacy_llm: он работает без контракта ML 2.0.
    contract_version: str | None
    # None — проверить конвейер нечем, не запуская анализ.
    pipeline_healthy: bool | None
    queue: QueueProbe

    @property
    def status(self) -> AnalysisHealthStatus:
        if self.pipeline_healthy is False or self.queue.consumers == 0:
            return AnalysisHealthStatus.DOWN
        if self.pipeline_healthy is None or self.queue.consumers is None:
            return AnalysisHealthStatus.UNKNOWN
        return AnalysisHealthStatus.OK
