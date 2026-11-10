"""Порт очереди фоновых задач анализа."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from app.domain.analysis_health import QueueProbe


class AnalysisQueue(Protocol):
    async def enqueue(self, job_id: uuid.UUID, source_ids: Sequence[uuid.UUID]) -> str:
        """Поставить анализ в очередь и вернуть идентификатор задачи в очереди.

        Бросает исключение, если очередь недоступна.
        """
        ...

    async def revoke(self, task_id: str) -> None:
        """Отозвать задачу, если она ещё не начала выполняться."""
        ...

    async def probe(self) -> QueueProbe:
        """Очередь, в которую уходит анализ, и число воркеров, которые её слушают.

        Не бросает исключений: недоступный брокер — consumers=None.
        """
        ...
