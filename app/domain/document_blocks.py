"""Правила построения блоков документа, общие для всех форматов.

Парсер формата знает только границы и тип фрагмента; ref, позицию и признак
editable назначает сборщик, чтобы одинаковые блоки из .docx, .md и .txt
получали одинаковые идентификаторы и одинаковые ограничения на правку.
"""

from app.domain.interfaces.document_parser import DocumentBlockType, ParsedBlock

# Префиксы ref короткие: ref уходит в каждое предложение и в каждый запрос к ML.
_REF_PREFIX: dict[DocumentBlockType, str] = {
    DocumentBlockType.HEADING: "h",
    DocumentBlockType.PARAGRAPH: "p",
    DocumentBlockType.LIST_ITEM: "li",
    DocumentBlockType.CODE: "c",
    DocumentBlockType.TABLE: "t",
    DocumentBlockType.OTHER: "o",
}

# Заголовки, код и таблицы экспортёры не умеют править без потери структуры,
# поэтому ML получает их только как контекст.
_READ_ONLY_TYPES = frozenset(
    {DocumentBlockType.HEADING, DocumentBlockType.CODE, DocumentBlockType.TABLE}
)


def block_ref(block_type: DocumentBlockType, position: int) -> str:
    return f"{_REF_PREFIX[block_type]}-{position}"


def heading_title(heading_text: str) -> str:
    """Название раздела по тексту блока-заголовка: «## Тарифы» → «Тарифы».

    Одно правило для разделов редактора и section_ref правок: фронт сопоставляет их
    по строке, и любое расхождение в обрезке оставит правку без раздела.
    """
    return heading_text.lstrip("#").strip()


class DocumentBlocksBuilder:
    """Накопитель блоков с проверкой инвариантов за O(1) на блок.

    Инварианты: границы лежат внутри plain_text, блоки идут по возрастанию
    и не пересекаются, пустые блоки не создаются.
    """

    __slots__ = ("_blocks", "_last_end", "_text_length")

    def __init__(self, text_length: int) -> None:
        self._text_length = text_length
        self._blocks: list[ParsedBlock] = []
        self._last_end = 0

    def add(
        self,
        block_type: DocumentBlockType,
        start_offset: int,
        end_offset: int,
        heading_level: int | None = None,
    ) -> None:
        if start_offset >= end_offset:
            return
        if start_offset < self._last_end or end_offset > self._text_length:
            raise ValueError(
                f"Блок [{start_offset}, {end_offset}) нарушает порядок или границы текста"
            )
        position = len(self._blocks)
        self._blocks.append(
            ParsedBlock(
                ref=block_ref(block_type, position),
                type=block_type,
                position=position,
                start_offset=start_offset,
                end_offset=end_offset,
                editable=block_type not in _READ_ONLY_TYPES,
                heading_level=heading_level if block_type is DocumentBlockType.HEADING else None,
            )
        )
        self._last_end = end_offset

    def build(self) -> list[ParsedBlock]:
        return self._blocks
