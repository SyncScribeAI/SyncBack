"""Ключи объектов в файловом хранилище.

Имя файла от пользователя в ключ не попадает: оно хранится в колонке name,
а объект называется по идентификатору записи. Расширение сохраняется,
потому что по нему выбирается парсер. Так в ключе не бывает путей,
управляющих символов и спецсимволов, которые ломают presigned-ссылки.
"""

from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath

_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


def file_extension(filename: str) -> str:
    """Расширение в нижнем регистре вместе с точкой или пустая строка, если его нет."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    suffix = PurePosixPath(name).suffix.lower()
    return suffix if _EXTENSION_RE.match(suffix) else ""


def document_storage_key(project_id: uuid.UUID, document_id: uuid.UUID, filename: str) -> str:
    return f"projects/{project_id}/documents/{document_id}{file_extension(filename)}"


def source_storage_key(project_id: uuid.UUID, source_id: uuid.UUID, filename: str) -> str:
    return f"projects/{project_id}/sources/{source_id}{file_extension(filename)}"


def document_layout_key(storage_key: str) -> str:
    """Кэш разбора лежит рядом с файлом документа и удаляется вместе с ним.

    Ключ не зависит от версии парсера и хэша файла: оба записаны внутри объекта и
    сверяются при чтении, а устаревший разбор перезаписывается.
    """
    return f"{storage_key}.layout"


def exported_document_key(storage_key: str) -> str:
    """Свой ключ на каждую сборку экспорта.

    Сборка загружается до фиксации транзакции ревью. С постоянным ключом откат оставил
    бы в БД прежний ключ, но под ним уже лежал бы новый, не утверждённый файл.
    """
    return f"{storage_key}.exported.{uuid.uuid4().hex}"


def derived_document_keys(storage_key: str | None) -> list[str]:
    """Объекты, которые сервис создаёт из файла документа; сейчас это кэш разбора PDF.

    Для ключей источников и других объектов список пуст: кэш разбора есть только у
    документов.
    """
    if storage_key and "/documents/" in storage_key and storage_key.lower().endswith(".pdf"):
        return [document_layout_key(storage_key)]
    return []
