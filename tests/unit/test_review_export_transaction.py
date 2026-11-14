"""Экспорт при завершении ревью фиксируется одной транзакцией вместе с решениями."""

from __future__ import annotations

import uuid
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from app.domain.services.document_export_service import DocumentExportService
from app.domain.services.suggestion_service import SuggestionService
from app.domain.value_objects import DocumentStatusVO, SuggestionStatusVO

pytestmark = pytest.mark.anyio

PREVIOUS_KEY = "projects/p/documents/d.docx.exported"


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {"projects/p/documents/d.docx": b"raw"}
        self.objects[PREVIOUS_KEY] = b"old export"

    async def download(self, key: str) -> bytes:
        return self.objects[key]

    async def upload(self, key: str, content: Any) -> None:
        self.objects[key] = b"new export"

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class _Documents:
    def __init__(self, uow: _UnitOfWork, document: Any) -> None:
        self._uow = uow
        self._document = document

    async def get_by_id(self, document_id: uuid.UUID) -> Any:
        return self._document

    async def compare_and_increment_review_version(self, document_id: uuid.UUID, expected: int):
        self._uow.pending.append(("review_version", expected + 1))
        return self._document

    async def update_exported_key(self, document: Any, export_key: str) -> None:
        self._uow.pending.append(("exported_storage_key", export_key))

    async def update_status(self, document: Any, status: DocumentStatusVO) -> Any:
        self._uow.pending.append(("status", status))
        return document


class _Suggestions:
    def __init__(self, uow: _UnitOfWork) -> None:
        self._uow = uow

    async def bulk_update_status(self, decisions: Any) -> int:
        self._uow.pending.append(("decisions", len(decisions.decisions)))
        return len(decisions.decisions)

    async def count_by_analysis_job_and_status(self, job_id: uuid.UUID, status: Any) -> int:
        return 0

    async def list_by_analysis_job_and_status_page(self, *args: Any, **kwargs: Any) -> list:
        # Принятая правка видна только внутри транзакции, где записано решение.
        if not any(name == "decisions" for name, _ in self._uow.pending):
            return []
        return [
            SimpleNamespace(
                section_ref="s1",
                change_type=SuggestionStatusVO.ACCEPTED,
                original_text="old",
                edited_text=None,
                suggested_text="new",
                start_offset=None,
                end_offset=None,
                geometry=None,
            )
        ]


class _UnitOfWork:
    """Записи копятся в pending и попадают в committed только при commit."""

    def __init__(self, document: Any) -> None:
        self.documents = _Documents(self, document)
        self.suggestions = _Suggestions(self)
        self.pending: list[tuple[str, Any]] = []
        self.committed: list[tuple[str, Any]] = []
        self.commits = 0

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        self.commits += 1
        self.committed.extend(self.pending)
        self.pending.clear()

    async def rollback(self) -> None:
        self.pending.clear()


class _Exporter:
    def __init__(self) -> None:
        self.changes: list = []

    def apply_changes(self, raw: bytes, changes: list) -> bytes:
        self.changes = list(changes)
        return b"new export"


@pytest.fixture()
def env() -> SimpleNamespace:
    document = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        current_analysis_job_id=uuid.uuid4(),
        status=DocumentStatusVO.AWAITING_APPROVAL,
        review_version=0,
        storage_key="projects/p/documents/d.docx",
        exported_storage_key=PREVIOUS_KEY,
        format="docx",
    )
    uow = _UnitOfWork(document)
    storage = _Storage()
    exporter = _Exporter()
    registry = SimpleNamespace(get_exporter=lambda fmt: exporter)
    export_service = DocumentExportService(uow, storage, registry)  # type: ignore[arg-type]
    service = SuggestionService(uow)  # type: ignore[arg-type]
    return SimpleNamespace(
        document=document,
        uow=uow,
        storage=storage,
        exporter=exporter,
        export_service=export_service,
        service=service,
    )


async def _save(env: SimpleNamespace) -> Any:
    return await env.service.atomic_review_save(
        project_id=env.document.project_id,
        document_id=env.document.id,
        user_id=uuid.uuid4(),
        review_version=0,
        accepted_ids=(uuid.uuid4(),),
        finalize=True,
        export_service=env.export_service,
    )


async def test_failure_after_export_rolls_back_decisions_and_export_key(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_finish(document: Any) -> Any:
        raise RuntimeError("сбой после экспорта")

    monkeypatch.setattr(env.service, "_finish_review", broken_finish)

    with pytest.raises(RuntimeError):
        await _save(env)

    assert env.uow.commits == 0
    assert env.uow.committed == []
    # БД по-прежнему указывает на прежний файл, и он не перезаписан.
    assert env.storage.objects[PREVIOUS_KEY] == b"old export"
    assert set(env.storage.objects) == {"projects/p/documents/d.docx", PREVIOUS_KEY}


async def test_successful_review_commits_once_with_export_key(env: SimpleNamespace) -> None:
    result = await _save(env)

    assert result.finalized is True
    assert env.uow.commits == 1
    names = [name for name, _ in env.uow.committed]
    assert names == ["review_version", "decisions", "exported_storage_key", "status"]
    new_key = dict(env.uow.committed)["exported_storage_key"]
    assert new_key != PREVIOUS_KEY
    assert env.storage.objects[new_key] == b"new export"
    assert PREVIOUS_KEY not in env.storage.objects
    # В файл попала правка, решение по которой ещё не было зафиксировано.
    assert len(env.exporter.changes) == 1
