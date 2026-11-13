"""Чтение снимков источников для просмотра и подсветки оснований правок."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.exceptions import SourceSnapshotNotFoundError
from app.domain.interfaces.file_storage import FileStorage
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.source_snapshot import SourceSnapshot


@dataclass(frozen=True, slots=True)
class SnapshotContent:
    snapshot: SourceSnapshot
    text: str


class SourceSnapshotService:
    def __init__(self, uow: IUnitOfWork, file_storage: FileStorage) -> None:
        self._uow = uow
        self._storage = file_storage

    async def get_content(self, project_id: uuid.UUID, snapshot_id: uuid.UUID) -> SnapshotContent:
        """Текст снимка ровно таким, каким он ушёл в конвейер; смещения цитат считаются по нему."""
        async with self._uow:
            snapshot = await self._uow.snapshots.get_in_project(project_id, snapshot_id)
        if snapshot is None:
            raise SourceSnapshotNotFoundError(f"Снимок {snapshot_id} не найден в проекте")
        raw = await self._storage.download(snapshot.storage_key)
        return SnapshotContent(snapshot=snapshot, text=raw.decode("utf-8"))
