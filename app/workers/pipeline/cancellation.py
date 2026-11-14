"""Прерывание шагов задачи анализа, когда пользователь её отменил.

Статус cancelled пишет API, а воркер в это время ждёт источники или ответ ML. Поэтому
шаг выполняется отдельной корутиной, а воркер периодически читает статус задачи и
снимает шаг, как только видит отмену. Снятие закрывает HTTP-соединение с ML, но не
останавливает обработку на стороне сервиса: это отражает ml_request_abandoned.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

logger = logging.getLogger("syncscribe.workers.cancellation")


class AnalysisCancelled(Exception):
    """Задача отменена; воркер прекращает её без записи результата и без повторов."""

    def __init__(self, *, ml_request_abandoned: bool = False) -> None:
        super().__init__("Анализ отменён")
        self.ml_request_abandoned = ml_request_abandoned


async def run_unless_cancelled[T](
    step: Coroutine[Any, Any, T],
    is_cancelled: Callable[[], Awaitable[bool]],
    poll_seconds: float,
    *,
    ml_request: bool = False,
) -> T:
    """Результат step или AnalysisCancelled, если задачу отменили раньше.

    ml_request: step отправляет запрос в ML. Если он снят до ответа, сервис мог продолжить
    вычисление, и исключение несёт ml_request_abandoned=True.
    """
    task = asyncio.ensure_future(step)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_seconds)
            if done:
                return task.result()
            if await _probe(is_cancelled):
                task.cancel()
                # Ждём выхода шага: httpx закрывает соединение при снятии корутины.
                (outcome,) = await asyncio.gather(task, return_exceptions=True)
                raise AnalysisCancelled(
                    ml_request_abandoned=ml_request and isinstance(outcome, BaseException)
                )
    finally:
        if not task.done():
            task.cancel()


async def _probe(is_cancelled: Callable[[], Awaitable[bool]]) -> bool:
    # Сбой чтения статуса не должен ронять анализ: следующая проверка повторит попытку.
    try:
        return await is_cancelled()
    except Exception:
        logger.warning("Не удалось проверить отмену задачи анализа", exc_info=True)
        return False
