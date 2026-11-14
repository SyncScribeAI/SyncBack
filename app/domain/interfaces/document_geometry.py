from collections.abc import Sequence
from typing import Protocol

from app.domain.document_geometry import DocumentLayout, PageRect, TextRuns


class DocumentGeometry(Protocol):
    """Перевод диапазонов plain_text в прямоугольники страниц.

    Есть только у форматов с фиксированной вёрсткой; для остальных реестр отдаёт None.
    """

    @property
    def version(self) -> str:
        """Версия эвристик разбора: от неё зависят блоки, поэтому по ней сбрасывается кэш."""
        ...

    def parse_layout(self, raw_bytes: bytes) -> DocumentLayout:
        """Тот же разбор, что parse(), карта текста к символам страниц и размеры страниц."""
        ...

    def locate(
        self, raw_bytes: bytes, runs: TextRuns, spans: Sequence[tuple[int, int]]
    ) -> list[tuple[PageRect, ...]]:
        """Прямоугольники каждого диапазона; открываются только затронутые страницы."""
        ...
