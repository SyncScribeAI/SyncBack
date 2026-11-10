"""AnalysisFlagService: какой флаг и в каком статусе документа можно отметить."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.domain.exceptions import (
    AnalysisFlagNotFoundError,
    DocumentNotFoundError,
    InvalidDocumentStatusError,
    StaleAnalysisFlagJobError,
)
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.services.analysis_flag_service import AnalysisFlagService
from app.domain.value_objects import DocumentStatusVO, FlagStatusVO
from tests.unit._fakes import FakeUnitOfWork

pytestmark = pytest.mark.asyncio

PROJECT = uuid.uuid4()
JOB = uuid.uuid4()
USER = uuid.uuid4()


def _setup(
    *,
    status: DocumentStatusVO = DocumentStatusVO.AWAITING_APPROVAL,
    flag_job: uuid.UUID = JOB,
    flag_document: uuid.UUID | None = None,
    changed: bool = True,
) -> tuple[AnalysisFlagService, FakeUnitOfWork, Any]:
    document = SimpleNamespace(
        id=uuid.uuid4(), project_id=PROJECT, status=status, current_analysis_job_id=JOB
    )
    flag = SimpleNamespace(
        id=uuid.uuid4(),
        analysis_job_id=flag_job,
        document_id=flag_document or document.id,
        status="pending",
    )
    flags = SimpleNamespace(
        get_by_id=AsyncMock(return_value=flag),
        set_status=AsyncMock(return_value=changed),
        list_by_analysis_job=AsyncMock(return_value=[flag]),
    )
    uow = FakeUnitOfWork(
        documents=SimpleNamespace(get_by_id=AsyncMock(return_value=document)), flags=flags
    )
    return AnalysisFlagService(cast(IUnitOfWork, uow)), uow, (document, flag)


async def _mark(service: AnalysisFlagService, objects: Any) -> Any:
    document, flag = objects
    return await service.set_status(PROJECT, document.id, flag.id, FlagStatusVO.REVIEWED, USER)


@pytest.mark.parametrize("status", [DocumentStatusVO.AWAITING_APPROVAL, DocumentStatusVO.READY])
async def test_flag_of_current_analysis_is_marked(status: DocumentStatusVO) -> None:
    service, uow, objects = _setup(status=status)
    change = await _mark(service, objects)
    assert (change.flag, change.changed, uow.committed) == (objects[1], True, True)
    uow.flags.set_status.assert_awaited_once_with(objects[1], FlagStatusVO.REVIEWED, USER)


async def test_repeated_mark_reports_no_change() -> None:
    service, _, objects = _setup(changed=False)
    assert (await _mark(service, objects)).changed is False


async def test_flag_of_previous_analysis_is_stale() -> None:
    service, uow, objects = _setup(flag_job=uuid.uuid4())
    with pytest.raises(StaleAnalysisFlagJobError):
        await _mark(service, objects)
    uow.flags.set_status.assert_not_awaited()


async def test_flag_of_other_document_looks_missing() -> None:
    service, uow, objects = _setup(flag_document=uuid.uuid4())
    with pytest.raises(AnalysisFlagNotFoundError):
        await _mark(service, objects)
    uow.flags.set_status.assert_not_awaited()


@pytest.mark.parametrize("status", [DocumentStatusVO.DRAFT, DocumentStatusVO.IN_PROGRESS])
async def test_document_without_results_rejects_mark(status: DocumentStatusVO) -> None:
    service, uow, objects = _setup(status=status)
    with pytest.raises(InvalidDocumentStatusError):
        await _mark(service, objects)
    assert uow.committed is False


async def test_document_of_other_project_is_missing() -> None:
    service, _, objects = _setup()
    with pytest.raises(DocumentNotFoundError):
        await service.list_for_document(uuid.uuid4(), objects[0].id)


async def test_document_without_analysis_has_no_flags() -> None:
    service, uow, objects = _setup()
    objects[0].current_analysis_job_id = None
    assert await service.list_for_document(PROJECT, objects[0].id) == []
    uow.flags.list_by_analysis_job.assert_not_awaited()
