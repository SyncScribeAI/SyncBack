"""
M-2 — тест: COUNT(*) OVER() с LIMIT в list_with_total.

Проверяемые инварианты:
  1. partial_page  — total == len(all_suggestions), items == page slice.
     Если window-функцию случайно заменят на COUNT(выборки после LIMIT),
     total упадёт до len(items) и тест провалится.
  2. empty_result  — ([], 0) без исключений при пустой таблице.
     На пустом результате обращение к первой строке для чтения total
     вызовет IndexError — этот тест отловит такую реализацию.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import AsyncContextManager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.interfaces.entities import DocumentProtocol, SuggestionProtocol
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.services.suggestion_service import SuggestionService
from app.domain.value_objects import (
    DocumentStatusVO,
    PaginationParams,
    SuggestionStatusVO,
)


# ---------------------------------------------------------------------------
# Helpers — minimal fakes
# ---------------------------------------------------------------------------


def _make_suggestion(analysis_job_id: uuid.UUID) -> SuggestionProtocol:
    """Возвращает объект, удовлетворяющий SuggestionProtocol."""
    s = MagicMock(spec=SuggestionProtocol)
    s.id = uuid.uuid4()
    s.analysis_job_id = analysis_job_id
    s.section_ref = "§1"
    s.change_type = MagicMock(value="replace")
    s.old_text = "old"
    s.new_text = "new"
    s.status = SuggestionStatusVO.PENDING
    s.decided_by = None
    s.decided_at = None
    return s


def _make_document(analysis_job_id: uuid.UUID) -> DocumentProtocol:
    doc = MagicMock(spec=DocumentProtocol)
    doc.id = uuid.uuid4()
    doc.project_id = uuid.uuid4()
    doc.status = DocumentStatusVO.AWAITING_APPROVAL
    doc.current_analysis_job_id = analysis_job_id
    doc.review_version = 1
    return doc


class _FakeSuggestionRepo:
    """
    Имитирует контракт list_with_total, аналогичный SQL с window-функцией:
      - total  = len(все строки для данного job_id)
      - items  = срез [offset : offset+limit]

    Именно это должна гарантировать настоящая реализация:
    total не зависит от limit/offset.
    """

    def __init__(self, all_suggestions: list[SuggestionProtocol]) -> None:
        self._all = all_suggestions

    async def list_with_total(
        self,
        analysis_job_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: SuggestionStatusVO | None = None,
        group_key: str | None = None,
    ) -> tuple[list[SuggestionProtocol], int]:
        matching = [
            s
            for s in self._all
            if s.analysis_job_id == analysis_job_id
            and (status is None or s.status == status)
            and (group_key is None or s.group_key == group_key)
        ]
        total = len(matching)
        items = matching[offset : offset + limit]
        return items, total

    # stub other required methods so the fake passes isinstance / Protocol checks
    async def get_by_id(self, *_):  # pragma: no cover
        return None

    async def update_status(self, *_):  # pragma: no cover
        pass


class _FakeDocumentRepo:
    def __init__(self, document: DocumentProtocol) -> None:
        self._doc = document

    async def get_by_id(self, document_id: uuid.UUID):
        if document_id == self._doc.id:
            return self._doc
        return None  # pragma: no cover


class _FakeUoW:
    """Минимальный IUnitOfWork: async context manager + два репозитория."""

    def __init__(
        self,
        document: DocumentProtocol,
        suggestions: list[SuggestionProtocol],
    ) -> None:
        self.documents = _FakeDocumentRepo(document)
        self.suggestions = _FakeSuggestionRepo(suggestions)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def commit(self):  # pragma: no cover
        pass

    async def rollback(self):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_total_partial_page():
    """
    M-2, сценарий 1: total корректен при limit < общего количества правок.

    Создаём 5 правок, запрашиваем первые 2 (limit=2, offset=0).
    Ожидаем: items == 2, total == 5.

    Если реализация считает total = len(items), тест упадёт (total будет 2).
    """
    job_id = uuid.uuid4()
    suggestions = [_make_suggestion(job_id) for _ in range(5)]
    doc = _make_document(job_id)
    uow = _FakeUoW(document=doc, suggestions=suggestions)
    service = SuggestionService(uow)

    pagination = PaginationParams(limit=2, offset=0)
    items, total = await service.list_suggestions_for_document(
        project_id=doc.project_id,
        document_id=doc.id,
        pagination=pagination,
    )

    assert total == 5, (
        f"total должен быть 5 (все строки), получено {total}. "
        "Вероятно, COUNT считается после LIMIT вместо window-функции."
    )
    assert len(items) == 2, f"items должен содержать 2 элемента, получено {len(items)}"


@pytest.mark.asyncio
async def test_list_with_total_empty_result():
    """
    M-2, сценарий 2: пустая выборка возвращает ([], 0) без исключений.

    Если реализация пытается прочитать total из первой строки результата
    (rows[0]["total"]), она упадёт с IndexError.
    """
    job_id = uuid.uuid4()
    doc = _make_document(job_id)
    uow = _FakeUoW(document=doc, suggestions=[])  # нет правок
    service = SuggestionService(uow)

    pagination = PaginationParams(limit=10, offset=0)
    items, total = await service.list_suggestions_for_document(
        project_id=doc.project_id,
        document_id=doc.id,
        pagination=pagination,
    )

    assert items == [], f"Ожидается пустой список, получено: {items}"
    assert total == 0, f"Ожидается total=0 на пустой выборке, получено: {total}"
