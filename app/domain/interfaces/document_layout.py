from collections.abc import Sequence
from typing import Protocol

from app.domain.document_geometry import DocumentLayout, PageRect


class DocumentLayoutReader(Protocol):
    """Разбор документа для редактора и анализа.

    Разбор PDF долгий и идёт под общим замком PDFium, поэтому реализация кэширует его
    рядом с файлом и выполняет вне цикла событий; остальные форматы разбираются сразу.
    """

    async def read(self, storage_key: str, raw_bytes: bytes) -> DocumentLayout: ...

    async def locate(
        self,
        storage_key: str,
        raw_bytes: bytes,
        layout: DocumentLayout,
        spans: Sequence[tuple[int, int]],
    ) -> list[tuple[PageRect, ...]] | None:
        """Прямоугольники диапазонов; None у форматов без страниц."""
        ...
