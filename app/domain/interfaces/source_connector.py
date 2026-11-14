"""
Порт коннектора источников истины.

После P2: SourceKind содержит только FILE и URL.
NOTE убран — тексты хранятся в файловом хранилище как .txt и обрабатываются
через ManualUploadConnector (SourceKind.FILE).

Конвертация ORM SourceType → SourceKind выполняется на границе
(analysis_tasks.py), а не внутри этого порта.
"""

import enum
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.source_text import SourceLayout


class SourceKind(enum.StrEnum):
    FILE = "file"
    URL = "url"


@dataclass
class SourceRef:
    id: uuid.UUID
    name: str
    type: SourceKind
    storage_key: str | None
    url: str | None
    uploaded_at: datetime


@dataclass
class SourceMetadata:
    name: str
    type: SourceKind
    uploaded_at: str


class SourceConnector(Protocol):
    async def fetch(self, source: SourceRef) -> str: ...

    async def fetch_structured(self, source: SourceRef) -> str:
        """Текст источника в едином формате для ML (см. app.domain.source_text).

        fetch остаётся для текущего LLM-конвейера: его промпт рассчитан на прежний текст.
        """
        ...

    async def fetch_layout(self, source: SourceRef) -> SourceLayout:
        """Текст fetch_structured и положение его фрагментов в исходном файле.

        Положение нужно подсветке основания в оригинале; коннектор, который его не знает,
        возвращает текст с пустым spans.
        """
        ...

    async def get_metadata(self, source: SourceRef) -> SourceMetadata: ...
    def supports(self, source_type: SourceKind) -> bool: ...


# Выбор коннектора по типу источника; сборка коннекторов остаётся в точках сборки.
SourceConnectorResolver = Callable[[SourceKind], SourceConnector]
