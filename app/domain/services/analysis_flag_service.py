"""Флаги «проверьте вручную»: чтение для редактора и отметка «просмотрено».

Флаг не правка: он не попадает в review-буфер, экспорт и счётчики правок и не мешает
документу перейти в ready. Отметка — только память для пользователя, что место он уже
сверил с цитатами. Поэтому её можно ставить и снимать и в awaiting_approval, и в ready —
везде, где у документа есть результаты анализа.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.exceptions import (
    AnalysisFlagNotFoundError,
    DocumentNotFoundError,
    InvalidDocumentStatusError,
    StaleAnalysisFlagJobError,
)
from app.domain.interfaces.entities import AnalysisFlagProtocol, DocumentProtocol
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.lifecycle import DocumentLifecycle
from app.domain.value_objects import FlagStatusVO


@dataclass(frozen=True, slots=True)
class FlagStatusChange:
    flag: AnalysisFlagProtocol
    # False — флаг уже был в этом статусе, событие аудита не нужно.
    changed: bool


class AnalysisFlagService:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def list_for_document(
        self, project_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[AnalysisFlagProtocol]:
        """Флаги текущего анализа документа в порядке ответа конвейера."""
        async with self._uow:
            document = await self._get_document(project_id, document_id)
            if document.current_analysis_job_id is None:
                return []
            return list(
                await self._uow.flags.list_by_analysis_job(document.current_analysis_job_id)
            )

    async def set_status(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        flag_id: uuid.UUID,
        status: FlagStatusVO,
        user_id: uuid.UUID,
    ) -> FlagStatusChange:
        async with self._uow:
            document = await self._get_document(project_id, document_id)
            flag = await self._uow.flags.get_by_id(flag_id)
            if flag is None or flag.document_id != document.id:
                raise AnalysisFlagNotFoundError(f"Флаг {flag_id} не найден")
            if flag.analysis_job_id != document.current_analysis_job_id:
                raise StaleAnalysisFlagJobError(
                    f"Флаг {flag_id} относится к прежнему анализу документа — "
                    "перезагрузите редактор."
                )
            if not DocumentLifecycle.has_review_results(document.status):
                raise InvalidDocumentStatusError(
                    f"Флаги нельзя отмечать в статусе документа {document.status.value}"
                )
            changed = await self._uow.flags.set_status(flag, status, user_id)
            await self._uow.commit()
        return FlagStatusChange(flag=flag, changed=changed)

    async def _get_document(
        self, project_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentProtocol:
        document = await self._uow.documents.get_by_id(document_id)
        if document is None or document.project_id != project_id:
            raise DocumentNotFoundError(f"Документ {document_id} не найден в проекте {project_id}")
        return document
