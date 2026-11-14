from collections.abc import Sequence
from typing import Protocol

from app.domain.document_geometry import PageRect, TextRuns
from app.domain.interfaces.document_parser import ParsedDocument


class DocumentGeometry(Protocol):
    """Перевод диапазонов plain_text в прямоугольники страниц.

    Есть только у форматов с фиксированной вёрсткой; для остальных реестр отдаёт None.
    """

    def parse_with_runs(self, raw_bytes: bytes) -> tuple[ParsedDocument, TextRuns]:
        """Тот же разбор, что parse(), и карта текста к символам страниц."""
        ...

    def locate(
        self, raw_bytes: bytes, runs: TextRuns, spans: Sequence[tuple[int, int]]
    ) -> list[tuple[PageRect, ...]]:
        """Прямоугольники каждого диапазона; открываются только затронутые страницы."""
        ...
