"""SQLAlchemy-адаптер для AnalysisFlag.

Фиксация транзакции — на стороне UoW. Статус меняется условным UPDATE, чтобы два
одновременных нажатия «Просмотрено» дали одно изменение и одно событие аудита.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.interfaces.repositories import IAnalysisFlagRepository
from app.domain.value_objects import FlagStatusVO
from app.infrastructure.db.models.analysis_flag import AnalysisFlag


class AnalysisFlagRepository(IAnalysisFlagRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_create(self, flags: Sequence[AnalysisFlag]) -> None:
        if not flags:
            return
        self._session.add_all(flags)
        await self._session.flush()

    async def get_by_id(self, flag_id: uuid.UUID) -> AnalysisFlag | None:
        return await self._session.get(AnalysisFlag, flag_id)

    async def list_by_analysis_job(self, analysis_job_id: uuid.UUID) -> list[AnalysisFlag]:
        result = await self._session.execute(
            select(AnalysisFlag)
            .where(AnalysisFlag.analysis_job_id == analysis_job_id)
            .order_by(AnalysisFlag.position.asc(), AnalysisFlag.id.asc())
        )
        return list(result.scalars().all())

    async def set_status(
        self, flag: AnalysisFlag, status: FlagStatusVO, user_id: uuid.UUID
    ) -> bool:
        # Возврат в pending стирает автора отметки, как сброс решения по правке;
        # кто вернул — видно в аудите.
        reviewed = status is FlagStatusVO.REVIEWED
        stmt = (
            update(AnalysisFlag)
            .where(AnalysisFlag.id == flag.id, AnalysisFlag.status != status.value)
            .values(
                status=status.value,
                decided_by=user_id if reviewed else None,
                decided_at=datetime.now(UTC) if reviewed else None,
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return bool(result.rowcount)

    async def reset_to_pending(self, analysis_job_id: uuid.UUID) -> int:
        stmt = (
            update(AnalysisFlag)
            .where(
                AnalysisFlag.analysis_job_id == analysis_job_id,
                AnalysisFlag.status != FlagStatusVO.PENDING.value,
            )
            .values(status=FlagStatusVO.PENDING.value, decided_by=None, decided_at=None)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)
