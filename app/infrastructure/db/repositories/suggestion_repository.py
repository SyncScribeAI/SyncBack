"""SQLAlchemy-адаптер для Suggestion.

Фиксация транзакции — на стороне UoW. Решения по правкам меняются условными
UPDATE (WHERE status = ...), чтобы параллельные запросы не перезаписывали друг друга.
Выборки упорядочены по (created_at, position, id). Правки одного анализа вставляются
одной транзакцией с одинаковым created_at, и порядок внутри неё задаёт position — номер
правки в ответе конвейера. created_at стоит первым ради прежнего конвейера: там каждый
источник пишет свои правки отдельной транзакцией со своей нумерацией, и без него правки
разных источников перемешались бы. В этом же порядке экспортёры применяют принятые правки.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, case, func, literal, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import SuggestionAlreadyDecidedError
from app.domain.interfaces.repositories import ISuggestionRepository
from app.domain.value_objects import ReviewDecisions, SuggestionDecision, SuggestionStatusVO
from app.infrastructure.db.models.enums import SuggestionStatus
from app.infrastructure.db.models.suggestion import Suggestion

_ORDER = (Suggestion.created_at.asc(), Suggestion.position.asc(), Suggestion.id.asc())


def _status_to_orm(vo: SuggestionStatusVO) -> SuggestionStatus:
    return SuggestionStatus(vo.value)


def _status_from_orm(orm_val: SuggestionStatus) -> SuggestionStatusVO:
    return SuggestionStatusVO(orm_val.value)


class SuggestionRepository(ISuggestionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_by_id(self, suggestion_id: uuid.UUID) -> Suggestion | None:
        return await self._session.get(Suggestion, suggestion_id)

    async def list_by_analysis_job(
        self,
        analysis_job_id: uuid.UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
        status: SuggestionStatusVO | None = None,
    ) -> list[Suggestion]:
        q = select(Suggestion).where(Suggestion.analysis_job_id == analysis_job_id)
        if status is not None:
            q = q.where(Suggestion.status == _status_to_orm(status))
        q = q.order_by(*_ORDER).offset(offset)
        if limit is not None:
            q = q.limit(limit)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def list_with_total(
        self,
        analysis_job_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: SuggestionStatusVO | None = None,
    ) -> tuple[list[Suggestion], int]:
        """Страница и total одним SELECT с оконной функцией.

        func.count().over() вычисляется ДО применения LIMIT/OFFSET в PostgreSQL,
        поэтому возвращает полный COUNT строк фильтрованной выборки.
        Если страница пуста (offset за пределами выборки), оконный счётчик недоступен,
        поэтому total читается отдельным COUNT с теми же фильтрами.
        """
        where_clauses = [Suggestion.analysis_job_id == analysis_job_id]
        if status is not None:
            where_clauses.append(Suggestion.status == _status_to_orm(status))

        rows = (
            await self._session.execute(
                select(Suggestion, func.count().over().label("total"))
                .where(*where_clauses)
                .order_by(*_ORDER)
                .limit(limit)
                .offset(offset)
            )
        ).all()

        if not rows:
            total = await self._session.scalar(
                select(func.count()).select_from(Suggestion).where(*where_clauses)
            )
            return [], total or 0
        items = [r[0] for r in rows]
        total = rows[0][1]
        return items, total

    async def count_by_analysis_job(self, analysis_job_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Suggestion)
            .where(Suggestion.analysis_job_id == analysis_job_id)
        )
        return result.scalar_one()

    async def count_by_analysis_job_and_status(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
    ) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Suggestion)
            .where(
                Suggestion.analysis_job_id == analysis_job_id,
                Suggestion.status == _status_to_orm(status),
            )
        )
        return result.scalar_one()

    async def list_by_analysis_job_and_status(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
    ) -> list[Suggestion]:
        result = await self._session.execute(
            select(Suggestion)
            .where(
                Suggestion.analysis_job_id == analysis_job_id,
                Suggestion.status == _status_to_orm(status),
            )
            .order_by(*_ORDER)
        )
        return list(result.scalars().all())

    async def list_by_analysis_job_and_status_page(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
        limit: int,
        offset: int = 0,
    ) -> list[Suggestion]:
        """
        Постраничный вариант list_by_analysis_job_and_status для стримингового
        экспорта. Покрывается индексом ix_suggestions_job_status (миграция 0015).
        """
        result = await self._session.execute(
            select(Suggestion)
            .where(
                Suggestion.analysis_job_id == analysis_job_id,
                Suggestion.status == _status_to_orm(status),
            )
            .order_by(*_ORDER)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_ids_by_analysis_job_and_status(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
    ) -> list[uuid.UUID]:
        result = await self._session.execute(
            select(Suggestion.id).where(
                Suggestion.analysis_job_id == analysis_job_id,
                Suggestion.status == _status_to_orm(status),
            )
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def bulk_create(self, suggestions: list[Suggestion]) -> list[Suggestion]:
        if not suggestions:
            return []
        self._session.add_all(suggestions)
        await self._session.flush()
        return suggestions

    async def update_status(
        self,
        suggestion: Suggestion,
        decision: SuggestionDecision,
    ) -> None:
        """
        Атомарный UPDATE ... WHERE status = 'pending'.
        Raises SuggestionAlreadyDecidedError если строка не затронута.
        """
        now = datetime.now(UTC)
        stmt = (
            update(type(suggestion))
            .where(
                type(suggestion).id == suggestion.id,
                type(suggestion).status == SuggestionStatus.PENDING,
            )
            .values(
                status=_status_to_orm(decision.status),
                decided_by=decision.user_id,
                decided_at=now,
            )
            .returning(type(suggestion).id)
        )
        result = await self._session.execute(stmt)
        updated = result.scalar_one_or_none()
        await self._session.flush()
        if updated is None:
            raise SuggestionAlreadyDecidedError("Правка уже была принята или отклонена")

    async def bulk_update_status(
        self,
        decisions: ReviewDecisions,
    ) -> int:
        """
        Один UPDATE ... WHERE id IN (...) AND status = 'pending'.

        Область ограничена документом: decisions.document_id сравнивается
        с денормализованной колонкой suggestions.document_id.

        edited_text пишется только тем решениям, где он передан; остальным — NULL,
        чтобы не осталось старой правки от предыдущего решения.
        """
        all_decisions = decisions.decisions
        if not all_decisions:
            return 0

        accepted_ids = [
            d.suggestion_id for d in all_decisions if d.status == SuggestionStatusVO.ACCEPTED
        ]
        all_ids = [d.suggestion_id for d in all_decisions]
        edits = [
            (d.suggestion_id, d.edited_text) for d in all_decisions if d.edited_text is not None
        ]

        where_clauses = [
            Suggestion.document_id == decisions.document_id,
            Suggestion.status == SuggestionStatus.PENDING,
            Suggestion.id.in_(all_ids),
        ]
        if decisions.analysis_job_id is not None:
            where_clauses.append(Suggestion.analysis_job_id == decisions.analysis_job_id)
        # Без явного типа литералы в CASE уходят в PostgreSQL как text, а колонка —
        # enum suggestion_status, и UPDATE падает. SQLite этого не замечает.
        status_type = Suggestion.__table__.c.status.type
        accepted = literal(_status_to_orm(SuggestionStatusVO.ACCEPTED), type_=status_type)
        rejected = literal(_status_to_orm(SuggestionStatusVO.REJECTED), type_=status_type)
        text_type = Suggestion.__table__.c.edited_text.type
        edited_text_expr = (
            case(
                *[(Suggestion.id == sid, literal(text, type_=text_type)) for sid, text in edits],
                else_=null(),
            )
            if edits
            else null()
        )
        stmt = (
            update(Suggestion)
            .where(*where_clauses)
            .values(
                status=case((Suggestion.id.in_(accepted_ids), accepted), else_=rejected),
                edited_text=edited_text_expr,
                decided_by=decisions.user_id,
                decided_at=func.now(),
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount

    async def bulk_accept_all(
        self,
        analysis_job_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[Suggestion]:
        """Принять все PENDING-правки одним UPDATE, вернуть обновлённые объекты."""
        now = datetime.now(UTC)
        stmt = (
            update(Suggestion)
            .where(
                Suggestion.analysis_job_id == analysis_job_id,
                Suggestion.status == SuggestionStatus.PENDING,
            )
            .values(
                status=SuggestionStatus.ACCEPTED,
                decided_by=user_id,
                decided_at=now,
            )
            .returning(Suggestion.id)
        )
        result = await self._session.execute(stmt)
        updated_ids = list(result.scalars().all())
        await self._session.flush()
        if not updated_ids:
            return []
        rows = await self._session.execute(select(Suggestion).where(Suggestion.id.in_(updated_ids)))
        return list(rows.scalars().all())

    async def bulk_reject_all(
        self,
        analysis_job_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[Suggestion]:
        """Отклонить все PENDING-правки одним UPDATE.

        Зеркало bulk_accept_all — один UPDATE WHERE status=PENDING,
        затем SELECT обновлённых объектов из identity map / БД.
        """
        now = datetime.now(UTC)
        stmt = (
            update(Suggestion)
            .where(
                Suggestion.analysis_job_id == analysis_job_id,
                Suggestion.status == SuggestionStatus.PENDING,
            )
            .values(
                status=SuggestionStatus.REJECTED,
                decided_by=user_id,
                decided_at=now,
            )
            .returning(Suggestion.id)
        )
        result = await self._session.execute(stmt)
        updated_ids = list(result.scalars().all())
        await self._session.flush()
        if not updated_ids:
            return []
        rows = await self._session.execute(select(Suggestion).where(Suggestion.id.in_(updated_ids)))
        return list(rows.scalars().all())

    async def reset_status(
        self,
        suggestion: Suggestion,
    ) -> Suggestion | None:
        """Сбросить решение правки обратно в PENDING.

        Атомарный UPDATE ... WHERE status != 'pending' AND id = ?.
        Возвращает обновлённый объект или None если правка уже PENDING
        (сбрасывать нечего — идемпотентно с точки зрения репозитория,
        но сервис вернёт SuggestionResetNotAllowedError).
        """
        stmt = (
            update(Suggestion)
            .where(
                Suggestion.id == suggestion.id,
                Suggestion.status != SuggestionStatus.PENDING,
            )
            .values(
                status=SuggestionStatus.PENDING,
                decided_by=None,
                decided_at=None,
                edited_text=None,
            )
            .returning(Suggestion.id)
        )
        result = await self._session.execute(stmt)
        updated_id = result.scalar_one_or_none()
        await self._session.flush()
        if updated_id is None:
            return None
        await self._session.refresh(suggestion)
        return suggestion

    async def reset_to_pending(
        self,
        analysis_job_id: uuid.UUID,
        ids: Sequence[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        where_clauses = [
            Suggestion.analysis_job_id == analysis_job_id,
            Suggestion.status != SuggestionStatus.PENDING,
        ]
        if ids is not None:
            if not ids:
                return []
            where_clauses.append(Suggestion.id.in_(list(ids)))
        stmt = (
            update(Suggestion)
            .where(*where_clauses)
            .values(
                status=SuggestionStatus.PENDING,
                decided_by=None,
                decided_at=None,
                edited_text=None,
            )
            .returning(Suggestion.id)
        )
        result = await self._session.execute(stmt)
        reset_ids = list(result.scalars().all())
        await self._session.flush()
        return reset_ids
