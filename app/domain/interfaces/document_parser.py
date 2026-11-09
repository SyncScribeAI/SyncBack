from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


@dataclass
class DocumentSection:
    ref: str
    start_offset: int
    end_offset: int


class DocumentBlockType(StrEnum):
    """Тип блока документа; значения совпадают с block_type_enum в БД."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    CODE = "code"
    TABLE = "table"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """Блок документа — единица, которую видит и правит ML-конвейер.

    Текст блока не хранится отдельно: он всегда равен plain_text[start_offset:end_offset],
    поэтому блоки не удваивают память под текст документа.
    """

    ref: str
    type: DocumentBlockType
    position: int
    start_offset: int
    end_offset: int
    editable: bool
    heading_level: int | None = None

    def text_in(self, plain_text: str) -> str:
        return plain_text[self.start_offset : self.end_offset]


@dataclass
class ParsedDocument:
    plain_text: str
    sections: list[DocumentSection] = field(default_factory=list)
    blocks: list[ParsedBlock] = field(default_factory=list)


class DocumentParser(Protocol):
    def parse(self, raw_bytes: bytes) -> ParsedDocument: ...


class IDocumentParserRegistry(Protocol):
    """Выбор парсера по имени файла. Неподдерживаемый формат → UnsupportedFormatError."""

    def parse_by_filename(self, filename: str, raw_bytes: bytes) -> ParsedDocument: ...
