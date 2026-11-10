"""Очередь анализа поверх Celery.

legacy_llm — chord «обработка источников → финализация»; остальные провайдеры — одна
задача analyze_document_for_job на документ (контракт ML 2.0). Порт AnalysisQueue
не меняется: API не знает, какой конвейер выполнит анализ.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from celery import chord

from app.domain.interfaces.analysis_queue import AnalysisQueue
from app.infrastructure.ml_pipeline.factory import uses_document_analyzer
from app.workers.celery_app import celery_app
from app.workers.tasks.analysis_tasks import finalize_analysis_job, process_source_for_analysis_job
from app.workers.tasks.analysis_v2_tasks import analyze_document_for_job


class CeleryAnalysisQueue(AnalysisQueue):
    async def enqueue(self, job_id: uuid.UUID, source_ids: Sequence[uuid.UUID]) -> str:
        # Отправка в брокер — блокирующий сетевой вызов, уводим его из event loop.
        return await asyncio.to_thread(self._send, str(job_id), [str(s) for s in source_ids])

    async def revoke(self, task_id: str) -> None:
        await asyncio.to_thread(celery_app.control.revoke, task_id, terminate=False)

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
