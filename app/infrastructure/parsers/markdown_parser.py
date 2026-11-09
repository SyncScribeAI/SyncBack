import re

from app.domain.document_blocks import DocumentBlocksBuilder, heading_title
from app.domain.interfaces.document_parser import (
    DocumentBlockType,
    DocumentSection,
    ParsedBlock,
    ParsedDocument,
)
from app.domain.text_normalization import normalize_text
from app.infrastructure.parsers.line_scanner import iter_lines

# Заголовок ищется в пределах строки: блок и раздел не продолжаются на следующую строку.
_HEADING_LINE_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+\S")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])[ \t]+\S")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_THEMATIC_BREAK_RE = re.compile(r"^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_TABLE_ROW_PREFIX = "|"


class MarkdownParser:
    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        text = self.extract_text(raw_bytes)
        blocks = _split_blocks(text)
        return ParsedDocument(plain_text=text, sections=_sections_from(blocks, text), blocks=blocks)

    def extract_source_text(self, raw_bytes: bytes) -> str:
        """Markdown уже в формате источника: разметка сохраняется как есть."""
        return self.extract_text(raw_bytes)

    def extract_text(self, raw_bytes: bytes) -> str:
        return normalize_text(raw_bytes.decode("utf-8", errors="replace"))


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


def _split_blocks(text: str) -> list[ParsedBlock]:
    """Один проход по строкам: абзацы по пустым строкам, заголовок и пункт списка —
    отдельные блоки, огороженный код — один блок целиком вместе с ограждением."""
    builder = DocumentBlocksBuilder(len(text))
    current_type: DocumentBlockType | None = None
    current_start = 0
    current_end = 0
    fence: str | None = None

    def flush() -> None:
        nonlocal current_type
        if current_type is not None:
            builder.add(current_type, current_start, current_end)
            current_type = None

    for line in iter_lines(text):
        if fence is not None:
            if not line.is_blank:
                current_end = line.content_end
            if line.text.lstrip().startswith(fence):
                fence = None
                flush()
            continue

        if line.is_blank:
            flush()
            continue

        fence_match = _FENCE_RE.match(line.text)
        if fence_match:
            flush()
            fence = fence_match.group(1)
            current_type = DocumentBlockType.CODE
            current_start, current_end = line.content_start, line.content_end
            continue

        heading_match = _HEADING_LINE_RE.match(line.text)
        if heading_match:
            flush()
            builder.add(
                DocumentBlockType.HEADING,
                line.content_start,
                line.content_end,
                heading_level=len(heading_match.group(1)),
            )
            continue

        if _THEMATIC_BREAK_RE.match(line.text):
            flush()
            continue

        if _LIST_ITEM_RE.match(line.text):
            flush()
            current_type = DocumentBlockType.LIST_ITEM
            current_start, current_end = line.content_start, line.content_end
            continue

        is_table_row = line.text.lstrip().startswith(_TABLE_ROW_PREFIX)
        if current_type is None:
            current_type = DocumentBlockType.TABLE if is_table_row else DocumentBlockType.PARAGRAPH
            current_start = line.content_start
        elif current_type is DocumentBlockType.TABLE and not is_table_row:
            flush()
            current_type = DocumentBlockType.PARAGRAPH
            current_start = line.content_start
        current_end = line.content_end

    # Незакрытое ограждение по CommonMark тянется до конца документа.
    flush()
    return builder.build()
