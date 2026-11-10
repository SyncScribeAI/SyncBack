"""Сервис аудит-лога: кто и что сделал с документом."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.value_objects import AuditActionVO

if TYPE_CHECKING:
    from app.infrastructure.db.models.audit_log import AuditLog


class AuditLogService:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def log_download(self, user_id: uuid.UUID, document_id: uuid.UUID) -> AuditLog:
        return await self._write(document_id, AuditActionVO.DOWNLOAD, user_id)

    async def log_suggestion_decision(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        suggestion_id: uuid.UUID,
        action: AuditActionVO,
    ) -> AuditLog:
        return await self._write(document_id, action, user_id, suggestion_id=suggestion_id)

    async def log_flag_decision(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        flag_id: uuid.UUID,
        action: AuditActionVO,
    ) -> AuditLog:
        # У флага нет строки в suggestions, поэтому событие — на уровне документа.
        return await self._write(document_id, action, user_id, details={"flag_id": str(flag_id)})

    async def log_suggestion_decisions(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        decisions: Iterable[tuple[uuid.UUID, AuditActionVO]],
    ) -> int:
        """Записать решения по набору правок одной транзакцией.

        Возвращает число созданных записей.
        """
        by_action: dict[AuditActionVO, list[uuid.UUID]] = defaultdict(list)
        for suggestion_id, action in decisions:
            by_action[action].append(suggestion_id)
        if not by_action:
            return 0
        written = 0
        async with self._uow:
            for action, suggestion_ids in by_action.items():
                written += await self._uow.audit.create_many(
                    document_id=document_id,
                    user_id=user_id,
                    action=action.value,
                    suggestion_ids=suggestion_ids,
                )
            await self._uow.commit()
        return written

    async def list_for_document(
        self,
        document_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        async with self._uow:
            return await self._uow.audit.list_for_document(document_id, limit=limit, offset=offset)

    async def _write(
        self,
        document_id: uuid.UUID,
        action: AuditActionVO,
        user_id: uuid.UUID | None,
        *,
        suggestion_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        async with self._uow:
            entry = await self._uow.audit.create(
                document_id=document_id,
                user_id=user_id,
                action=action.value,
                suggestion_id=suggestion_id,
                details=details,
            )
            await self._uow.commit()
        return entry
