"""Разбор Markdown по CommonMark с таблицами GFM.

Границы блоков берутся из markdown-it-py: регулярный разбор отдавал модели на правку
front matter, HTML и код с отступом как обычный абзац. Контракт прежний: plain_text равен
normalize_text(исходник), а смещения блоков указывают в сырую разметку, поэтому экспорт
и сохранённые правки работают с тем же текстом.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.domain.document_blocks import DocumentBlocksBuilder, heading_title
from app.domain.interfaces.document_parser import (
    DocumentBlockType,
    DocumentSection,
    ParsedBlock,
    ParsedDocument,
)
from app.domain.source_text import SourceLayout
from app.domain.text_normalization import normalize_text

_FRONT_MATTER_OPEN = "---"
_FRONT_MATTER_CLOSE = frozenset({"---", "..."})


class MarkdownParser:
    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        text = self.extract_text(raw_bytes)
        blocks = _split_blocks(text)
        return ParsedDocument(plain_text=text, sections=_sections_from(blocks, text), blocks=blocks)

    def extract_source_text(self, raw_bytes: bytes) -> str:
        """Markdown уже в формате источника: разметка сохраняется как есть."""
        return self.extract_text(raw_bytes)

    def extract_source_layout(self, raw_bytes: bytes) -> SourceLayout:
        return SourceLayout(self.extract_source_text(raw_bytes))

    def extract_text(self, raw_bytes: bytes) -> str:
        return normalize_text(raw_bytes.decode("utf-8", errors="replace"))


@functools.cache
def _markdown() -> MarkdownIt:
    # Экземпляр без состояния между вызовами parse, поэтому один на процесс.
    return MarkdownIt("commonmark").enable("table")


def _sections_from(blocks: list[ParsedBlock], text: str) -> list[DocumentSection]:
    """Разделы — это заголовочные блоки, поэтому «# …» внутри кода раздела не открывает."""
    headings = [b for b in blocks if b.type is DocumentBlockType.HEADING]
    sections: list[DocumentSection] = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start_offset if i + 1 < len(headings) else len(text)
        title = heading_title(heading.text_in(text))
        sections.append(
            DocumentSection(ref=title, start_offset=heading.start_offset, end_offset=end)
        )
    return sections


@dataclass(frozen=True, slots=True)
class _Lines:
    """Начала строк текста: перевод номера строки из token.map в смещение за O(1)."""

    text: str
    starts: list[int]

    @classmethod
    def of(cls, text: str) -> _Lines:
        starts = [0]
        position = text.find("\n")
        while position != -1:
            starts.append(position + 1)
            position = text.find("\n", position + 1)
        return cls(text, starts)

    @property
    def count(self) -> int:
        return len(self.starts)

    def line(self, index: int) -> str:
        return self.text[self.starts[index] : self.end_of(index)]

    def end_of(self, index: int) -> int:
        """Конец строки без перевода строки."""
        if index + 1 < len(self.starts):
            return self.starts[index + 1] - 1
        return len(self.text)

    def span(self, first: int, stop: int) -> tuple[int, int]:
        """Смещения строк [first, stop) без пробелов по краям; O(длины краевых пробелов)."""
        start = self.starts[first]
        end = self.end_of(stop - 1)
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        return start, end


def _front_matter_end(lines: _Lines) -> int:
    """Число строк front matter или 0.

    Предпроход нужен, потому что markdown-it разбирает front matter как разделитель и
    setext-заголовок второго уровня.
    """
    if lines.count < 2 or lines.line(0).rstrip() != _FRONT_MATTER_OPEN:
        return 0
    for index in range(1, lines.count):
        if lines.line(index).rstrip() in _FRONT_MATTER_CLOSE:
            return index + 1
    return 0


def _split_blocks(text: str) -> list[ParsedBlock]:
    """Блоки за один проход по токенам; O(n) по длине текста.

    Цитата раскрывается по вложенным блокам. Пункт списка строится по абзацам внутри
    него: диапазон строк list_item_open включает вложенные пункты, и блок по нему
    перекрыл бы их. Первый абзац пункта начинается с маркера.
    """
    lines = _Lines.of(text)
    builder = DocumentBlocksBuilder(len(text))
    shift = _front_matter_end(lines)
    if shift:
        start, end = lines.span(0, shift)
        builder.add(DocumentBlockType.OTHER, start, end, editable=False)
    if shift >= lines.count:
        return builder.build()

    rest = text[lines.starts[shift] :]
    tokens = _markdown().parse(rest)
    # Открытые контейнеры: для пункта списка — строка его маркера, пока первый абзац не взят.
    open_items: list[int | None] = []
    skip_until: str | None = None
    for index, token in enumerate(tokens):
        if skip_until is not None:
            if token.type == skip_until:
                skip_until = None
            continue
        kind = token.type
        if kind == "list_item_open":
            open_items.append(token.map[0] + shift if token.map else None)
        elif kind == "list_item_close":
            open_items.pop()
        elif kind == "paragraph_open" and token.map:
            first, stop = token.map[0] + shift, token.map[1] + shift
            if open_items:
                marker_line = open_items[-1]
                if marker_line is not None:
                    first = min(first, marker_line)
                    open_items[-1] = None
                _add(builder, lines, DocumentBlockType.LIST_ITEM, first, stop)
            else:
                _add(builder, lines, DocumentBlockType.PARAGRAPH, first, stop)
        elif kind == "heading_open" and token.map:
            _add_heading(builder, lines, token, tokens, index, shift)
        elif kind in ("fence", "code_block") and token.map:
            _add(builder, lines, DocumentBlockType.CODE, *_shifted(token, shift))
        elif kind == "table_open" and token.map:
            _add(builder, lines, DocumentBlockType.TABLE, *_shifted(token, shift))
            skip_until = "table_close"
        elif kind in ("html_block", "hr") and token.map:
            _add(builder, lines, DocumentBlockType.OTHER, *_shifted(token, shift), editable=False)
    return builder.build()


def _shifted(token: Token, shift: int) -> tuple[int, int]:
    assert token.map is not None
    return token.map[0] + shift, token.map[1] + shift


def _add(
    builder: DocumentBlocksBuilder,
    lines: _Lines,
    block_type: DocumentBlockType,
    first: int,
    stop: int,
    *,
    editable: bool | None = None,
    heading_level: int | None = None,
) -> None:
    start, end = lines.span(first, min(stop, lines.count))
    builder.add(block_type, start, end, heading_level=heading_level, editable=editable)


def _add_heading(
    builder: DocumentBlocksBuilder,
    lines: _Lines,
    token: Token,
    tokens: list[Token],
    index: int,
    shift: int,
) -> None:
    first, stop = _shifted(token, shift)
    inline = tokens[index + 1] if index + 1 < len(tokens) else None
    if inline is None or not inline.content.strip():
        # «#» без текста по CommonMark — пустой заголовок; раздела без названия не бывает.
        _add(builder, lines, DocumentBlockType.OTHER, first, stop, editable=False)
        return
    level = int(token.tag[1:]) if token.tag[1:].isdigit() else None
    _add(builder, lines, DocumentBlockType.HEADING, first, stop, heading_level=level)
