"""Периодическая сверка задач анализа, потерянных до отправки в брокер.

Сверка живёт в процессе API: он и так отправляет задачи в очередь, а отдельный
планировщик потребовал бы ещё один сервис в Compose. Несколько реплик API безопасны:
каждая задача берётся под блокировкой строки с SKIP LOCKED.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta

from app.core.config import Settings
from app.core.dependencies import _get_analysis_queue, get_event_publisher
from app.domain.services.analysis_job_service import AnalysisJobService
from app.infrastructure.db.session import get_sessionmaker
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

logger = logging.getLogger("syncscribe.analysis.reconciler")

ServiceFactory = Callable[[], AbstractAsyncContextManager[AnalysisJobService]]


@asynccontextmanager
async def _api_service() -> AsyncIterator[AnalysisJobService]:
    async with get_sessionmaker()() as session:
        yield AnalysisJobService(
            SqlAlchemyUnitOfWork(session),
            queue=_get_analysis_queue(),
            events=get_event_publisher(),
        )


async def reconcile_once(settings: Settings, service_factory: ServiceFactory) -> int:
    """Один проход сверки; сбой БД или брокера пишется в лог и не останавливает цикл."""
    try:
        async with service_factory() as service:
            return await service.requeue_lost_jobs(
                older_than=timedelta(seconds=settings.analysis_dispatch_stale_seconds),
                limit=settings.analysis_dispatch_reconcile_batch,
            )
    except Exception:
        logger.exception("Сверка очереди анализа не удалась")
        return 0


async def run_dispatch_reconciler(
    settings: Settings, service_factory: ServiceFactory = _api_service
) -> None:
    """Бесконечный цикл сверки; останавливается отменой задачи при остановке API."""
    interval = settings.analysis_dispatch_reconcile_interval_seconds
    while True:
        await asyncio.sleep(interval)
        await reconcile_once(settings, service_factory)


def start_dispatch_reconciler(settings: Settings) -> asyncio.Task[None] | None:
    """Запустить сверку фоном; интервал 0 отключает её, например на время отладки."""
    if settings.analysis_dispatch_reconcile_interval_seconds == 0:
        return None
    return asyncio.create_task(run_dispatch_reconciler(settings), name="dispatch-reconciler")


async def stop_dispatch_reconciler(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
