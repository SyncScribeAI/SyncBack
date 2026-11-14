"""
Порты для сборки финального документа с учётом принятых правок. Работают с лёгким DTO
AppliedChange, а не с ORM-моделью Suggestion напрямую.

start_offset/end_offset — границы old_text в plain_text документа (правки конвейера v2).
Экспортёр доверяет им, только если текст файла в этих границах совпадает с old_text;
иначе, как и для правок без границ, ищет первое вхождение.

rects нужны форматам с фиксированной вёрсткой (PDF): текст там не переписывается,
правка ставится отметкой поверх места на странице.
"""

from dataclasses import dataclass
from typing import Protocol

from app.domain.document_geometry import PageRect
from app.domain.value_objects import DocumentFormatVO


@dataclass
class AppliedChange:
    section_ref: str
    change_type: str
    old_text: str | None
    new_text: str | None
    start_offset: int | None = None
    end_offset: int | None = None
    rects: tuple[PageRect, ...] = ()

    @property
    def has_span(self) -> bool:
        return (
            self.start_offset is not None
            and self.end_offset is not None
            and 0 <= self.start_offset <= self.end_offset
        )


class DocumentExporter(Protocol):
    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes: ...


class DocumentExporterRegistry(Protocol):
    def get_exporter(self, document_format: DocumentFormatVO) -> DocumentExporter: ...
