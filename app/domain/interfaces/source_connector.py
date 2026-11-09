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
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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

    async def get_metadata(self, source: SourceRef) -> SourceMetadata: ...
    def supports(self, source_type: SourceKind) -> bool: ...
