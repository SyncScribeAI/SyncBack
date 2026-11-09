"""
ManualUploadConnector — скачивает файл из хранилища и парсит его в plain-text.

Обрабатывает только SourceKind.FILE: текстовые заметки сохраняются как .txt
и попадают сюда как обычные файлы.
"""

from app.domain.interfaces.file_storage import FileStorage
from app.domain.interfaces.source_connector import SourceKind, SourceMetadata, SourceRef
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry


class ManualUploadConnector:
    def __init__(self, file_storage: FileStorage, parser_registry: DocumentParserRegistry):
        self._storage = file_storage
        self._parsers = parser_registry

    def supports(self, source_type: SourceKind) -> bool:
        return source_type == SourceKind.FILE

    async def fetch(self, source: SourceRef) -> str:
        if source.type != SourceKind.FILE:
            raise ValueError(
                f"ManualUploadConnector поддерживает только FILE, получен: {source.type}"
            )
        if not source.storage_key:
            raise ValueError(f"storage_key отсутствует для источника {source.id}")
        raw_bytes = await self._storage.download(source.storage_key)
        return self._parsers.extract_text(source.storage_key, raw_bytes)

    async def get_metadata(self, source: SourceRef) -> SourceMetadata:
        return SourceMetadata(
            name=source.name,
            type=source.type,
            uploaded_at=source.uploaded_at.isoformat(),
        )
