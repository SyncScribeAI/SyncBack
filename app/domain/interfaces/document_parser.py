from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # source_text сам импортирует DocumentBlockType отсюда.
    from app.domain.interfaces.document_geometry import DocumentGeometry
    from app.domain.source_text import SourceLayout


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

    def extract_text(self, raw_bytes: bytes) -> str:
        """Тот же plain_text, что в parse(), но без разбора на блоки и разделы."""
        ...

    def extract_source_text(self, raw_bytes: bytes) -> str:
        """Текст файла как источника в едином формате (см. app.domain.source_text)."""
        ...

    def extract_source_layout(self, raw_bytes: bytes) -> "SourceLayout":
        """Тот же текст, что extract_source_text, и положение фрагментов в файле."""
        ...


class IDocumentParserRegistry(Protocol):
    """Выбор парсера по имени файла. Неподдерживаемый формат → UnsupportedFormatError.

    parse_by_filename — для редактора и запроса к ML, где нужны блоки; extract_text —
    для источников и кэша текста, где блоки были бы лишней работой и памятью.
    """

    def parse_by_filename(self, filename: str, raw_bytes: bytes) -> ParsedDocument: ...

    def extract_text(self, filename: str, raw_bytes: bytes) -> str: ...

    def extract_source_text(self, filename: str, raw_bytes: bytes) -> str: ...

    def extract_source_layout(self, filename: str, raw_bytes: bytes) -> "SourceLayout": ...

    def geometry_for(self, filename: str) -> "DocumentGeometry | None":
        """Перевод текста в прямоугольники страниц; None для форматов без вёрстки."""
        ...
