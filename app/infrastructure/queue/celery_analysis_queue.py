"""Очередь анализа поверх Celery.

legacy_llm — chord «обработка источников → финализация»; остальные провайдеры — одна
задача analyze_document_for_job на документ (контракт ML 2.0). Порт AnalysisQueue
не меняется: API не знает, какой конвейер выполнит анализ.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from celery import chord

from app.core.config import Settings, get_settings
from app.domain.analysis_health import QueueProbe
from app.domain.interfaces.analysis_queue import AnalysisQueue
from app.infrastructure.ml_pipeline.factory import uses_document_analyzer
from app.workers.celery_app import DEFAULT_QUEUE, celery_app
from app.workers.tasks.analysis_tasks import finalize_analysis_job, process_source_for_analysis_job
from app.workers.tasks.analysis_v2_tasks import analyze_document_for_job

logger = logging.getLogger("syncscribe.queue.analysis")

# Ответ на broadcast ждём недолго: проверку вызывает администратор из API, и минута
# ожидания хуже честного «неизвестно».
_INSPECT_TIMEOUT_SECONDS = 1.0


def analysis_queue_name(settings: Settings | None = None) -> str:
    """Очередь, в которую уходит анализ при текущем ANALYSIS_PROVIDER."""
    settings = settings or get_settings()
    return settings.analysis_v2_queue if uses_document_analyzer(settings) else DEFAULT_QUEUE


def count_consumers(active_queues: Mapping[str, Sequence[Mapping[str, Any]]], queue: str) -> int:
    """Число воркеров, у которых среди очередей есть queue. O(W·Q), W·Q — пары воркер-очередь."""
    return sum(
        1 for queues in active_queues.values() if any(q.get("name") == queue for q in queues)
    )


class CeleryAnalysisQueue(AnalysisQueue):
    async def enqueue(self, job_id: uuid.UUID, source_ids: Sequence[uuid.UUID]) -> str:
        # Отправка в брокер — блокирующий сетевой вызов, уводим его из event loop.
        return await asyncio.to_thread(self._send, str(job_id), [str(s) for s in source_ids])

    async def revoke(self, task_id: str) -> None:
        await asyncio.to_thread(celery_app.control.revoke, task_id, terminate=False)

    async def probe(self) -> QueueProbe:
        queue = analysis_queue_name()
        return QueueProbe(queue=queue, consumers=await asyncio.to_thread(self._consumers, queue))

    @staticmethod
    def _consumers(queue: str) -> int | None:
        try:
            reply = celery_app.control.inspect(timeout=_INSPECT_TIMEOUT_SECONDS).active_queues()
        except Exception as exc:
            logger.warning(
                "Очередь анализа: брокер недоступен", extra={"error_type": type(exc).__name__}
            )
            return None
        # Пустой ответ — ни один воркер не откликнулся: живых воркеров нет вовсе.
        return count_consumers(reply or {}, queue)

    @staticmethod
    def _send(job_id: str, source_ids: list[str]) -> str:
        if uses_document_analyzer():
            # Повторы Celery сохраняют task_id, поэтому revoke по нему работает на любой попытке.
            return str(analyze_document_for_job.delay(job_id, source_ids).id)
        if not source_ids:
            return str(finalize_analysis_job.delay([], job_id).id)
        header = [process_source_for_analysis_job.si(job_id, source_id) for source_id in source_ids]
        # Callback chord получает список результатов первым аргументом, поэтому .s(), а не .si()
        return str(chord(header)(finalize_analysis_job.s(job_id)).id)
