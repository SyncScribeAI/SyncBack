"""
Бизнес-логика проектов.

Архитектурные правила:
  - Зависит только от IUnitOfWork (порт) и FileStorage (порт).
  - Нет импортов из app.infrastructure.* при выполнении.
  - Один uow.commit() на операцию.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.domain.exceptions import ProjectNotFoundError
from app.domain.interfaces.file_storage import FileStorage
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.project_appearance import (
    default_project_color,
    normalize_project_color,
    normalize_project_icon,
)
from app.domain.source_snapshot import fingerprints_storage_key
from app.domain.value_objects import ProjectContentCounts, UserRoleVO

if TYPE_CHECKING:
    from app.infrastructure.db.models.project import Project
    from app.infrastructure.db.models.user import User


logger = logging.getLogger("syncscribe.projects")


class ProjectService:
    def __init__(self, uow: IUnitOfWork, file_storage: FileStorage) -> None:
        self._uow = uow
        self._storage = file_storage

    async def create_project(
        self,
        owner: User,
        name: str,
        description: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> Project:
        """Создать проект. Без цвета берётся следующий цвет палитры для этого владельца.

        Бросает InvalidProjectColorError, если цвет не из палитры.
        """
        normalized_color = normalize_project_color(color) if color is not None else None
        async with self._uow:
            if normalized_color is None:
                existing = await self._uow.projects.count_by_owner(owner.id)
                normalized_color = default_project_color(existing)
            project = await self._uow.projects.create(
                owner_id=owner.id,
                name=name,
                description=description,
                color=normalized_color,
                icon=normalize_project_icon(icon) if icon is not None else None,
            )
            await self._uow.commit()
        return project

    async def get_project_for_user(self, project_id: uuid.UUID, user: User) -> Project:
        """Проект, доступный пользователю: владельцу или администратору.

        Чужой проект намеренно неотличим от несуществующего (ProjectNotFoundError),
        чтобы не раскрывать факт его существования.
        """
        async with self._uow:
            project = await self._uow.projects.get_by_id(project_id)
        if project is None or (user.role != UserRoleVO.ADMIN and project.owner_id != user.id):
            raise ProjectNotFoundError(f"Проект {project_id} не найден")
        return project

    async def list_projects_for_user(
        self, user: User, limit: int, offset: int
    ) -> tuple[list[Project], int]:
        async with self._uow:
            if user.role == UserRoleVO.ADMIN:
                items = await self._uow.projects.list_all(limit=limit, offset=offset)
                total = await self._uow.projects.count_all()
            else:
                items = await self._uow.projects.list_by_owner(user.id, limit=limit, offset=offset)
                total = await self._uow.projects.count_by_owner(user.id)
        return items, total

    async def update_project(
        self,
        project: Project,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> Project:
        """Частичное обновление полей проекта; None означает «не менять», пустая иконка — сброс.

        Бросает InvalidProjectColorError, если цвет не из палитры.
        """
        async with self._uow:
            updated = await self._uow.projects.update(
                project,
                name=name,
                description=description,
                color=normalize_project_color(color) if color is not None else None,
                icon=_icon_update(icon),
            )
            await self._uow.commit()
        return updated

    async def content_counts(
        self, project_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ProjectContentCounts]:
        """Счётчики для карточек одним запросом на всю страницу проектов."""
        async with self._uow:
            return await self._uow.projects.count_contents(project_ids)

    async def delete_project(self, project: Project) -> None:
        """
        Удаление проекта: сначала БД, потом хранилище.
        1. Собираем все ключи хранилища проекта.
        2. Удаляем запись и фиксируем транзакцию — ON DELETE CASCADE убирает
           дочерние строки. При ошибке БД файлы остаются на месте.
        3. Удаляем объекты из хранилища (best-effort): при сбое остаются
           осиротевшие файлы без потери данных пользователя; ошибка логируется.
        """
        project_id = project.id
        async with self._uow:
            storage_keys = await self._uow.projects.collect_storage_keys(project_id)
            for key in await self._uow.snapshots.list_storage_keys(project_id):
                storage_keys += [key, fingerprints_storage_key(key)]
            await self._uow.projects.delete(project)
            await self._uow.commit()

        for key in storage_keys:
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Не удалось удалить файл %s после удаления проекта %s (осиротевший объект)",
                    key,
                    project_id,
                    exc_info=True,
                )


def _icon_update(icon: str | None) -> str | None:
    """Значение иконки для частичного обновления: None — не менять, "" — сбросить."""
    if icon is None:
        return None
    return normalize_project_icon(icon) or ""
