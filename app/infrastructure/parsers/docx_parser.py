import io
from collections.abc import Iterator
from typing import Any

from docx import Document as DocxDocument
from docx.text.paragraph import Paragraph

from app.domain.document_blocks import DocumentBlocksBuilder
from app.domain.interfaces.document_parser import (
    DocumentBlockType,
    DocumentSection,
    ParsedDocument,
)
from app.domain.source_text import SourceFragment, SourceLayout, serialize_source_layout
from app.domain.text_normalization import normalize_text
from app.infrastructure.docx_paragraphs import (
    iter_docx_body_items,
    iter_docx_paragraphs,
    iter_table_paragraphs,
    table_rows,
)

_HEADING_STYLE_PREFIX = "Heading"
_LIST_STYLE_PREFIX = "List"
# Вид локатора: диапазон порядковых номеров непустых абзацев файла, оба конца включены.
DOCX_PARAGRAPHS_LOCATOR = "docx_paragraphs"


class DocxParser:
    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        docx_document = DocxDocument(io.BytesIO(raw_bytes))
        sections: list[DocumentSection] = []
        text_parts: list[str] = []
        # Границы блоков копятся до сборки plain_text: сборщику нужна итоговая длина текста.
        block_spans: list[tuple[DocumentBlockType, int, int, int | None]] = []
        offset = 0
        for item in iter_docx_paragraphs(docx_document):
            paragraph = item.paragraph
            paragraph_text = normalize_text(paragraph.text)
            style_name = _style_name(paragraph)
            # Стиль заголовка внутри ячейки — оформление таблицы, а не раздел документа.
            is_heading = not item.in_table and style_name.startswith(_HEADING_STYLE_PREFIX)
            if is_heading:
                sections.append(
                    DocumentSection(
                        ref=paragraph_text.strip() or "Без названия",
                        start_offset=offset,
                        end_offset=offset,
                    )
                )
            content = paragraph_text.strip()
            if content:
                start = offset + (len(paragraph_text) - len(paragraph_text.lstrip()))
                if item.in_table:
                    block_type, level = DocumentBlockType.TABLE, None
                else:
                    block_type, level = _classify(paragraph, style_name)
                block_spans.append((block_type, start, start + len(content), level))
            text_parts.append(paragraph_text)
            offset += len(paragraph_text) + 1
        plain_text = "\n".join(text_parts)
        for i, section in enumerate(sections):
            section.end_offset = (
                sections[i + 1].start_offset if i + 1 < len(sections) else len(plain_text)
            )
        builder = DocumentBlocksBuilder(len(plain_text))
        for block_type, start, end, level in block_spans:
            builder.add(block_type, start, end, heading_level=level)
        return ParsedDocument(plain_text=plain_text, sections=sections, blocks=builder.build())

    def extract_source_text(self, raw_bytes: bytes) -> str:
        return self.extract_source_layout(raw_bytes).text

    def extract_source_layout(self, raw_bytes: bytes) -> SourceLayout:
        return serialize_source_layout(_iter_located_fragments(DocxDocument(io.BytesIO(raw_bytes))))

    def extract_text(self, raw_bytes: bytes) -> str:
        docx_document = DocxDocument(io.BytesIO(raw_bytes))
        return "\n".join(
            normalize_text(item.paragraph.text) for item in iter_docx_paragraphs(docx_document)
        )


def _iter_located_fragments(
    docx_document: Any,
) -> Iterator[tuple[SourceFragment, dict[str, Any]]]:
    """Фрагменты источника и диапазон порядковых номеров непустых абзацев файла. O(P).

    Номера считаются тем же обходом, что iter_docx_paragraphs, включая абзацы ячеек и
    вложенных таблиц: просмотр DOCX находит абзац по номеру среди непустых абзацев и
    проверяет его текстом. Таблица целиком один фрагмент, поэтому её диапазон покрывает
    все её абзацы.
    """
    ordinal = 0
    for item in iter_docx_body_items(docx_document):
        if isinstance(item, Paragraph):
            text = normalize_text(item.text)
            if not text.strip():
                continue
            block_type, level = _classify(item, _style_name(item))
            yield SourceFragment(block_type, text, level), _paragraphs_locator(ordinal, ordinal)
            ordinal += 1
            continue
        first = ordinal
        for cell_paragraph in iter_table_paragraphs(item):
            if normalize_text(cell_paragraph.paragraph.text).strip():
                ordinal += 1
        rows = [
            f"| {' | '.join(normalize_text(cell) for cell in cells)} |"
            for cells in table_rows(item)
            if any(cells)
        ]
        if rows and ordinal > first:
            yield (
                SourceFragment(DocumentBlockType.TABLE, "\n".join(rows)),
                _paragraphs_locator(first, ordinal - 1),
            )


def _paragraphs_locator(first: int, last: int) -> dict[str, Any]:
    return {"kind": DOCX_PARAGRAPHS_LOCATOR, "first": first, "last": last}


def _style_name(paragraph: Paragraph) -> str:
    return paragraph.style.name if paragraph.style is not None else ""


def _classify(paragraph: Paragraph, style_name: str) -> tuple[DocumentBlockType, int | None]:
    if style_name.startswith(_HEADING_STYLE_PREFIX):
        return DocumentBlockType.HEADING, _heading_level(style_name)
    if _is_list_item(paragraph, style_name):
        return DocumentBlockType.LIST_ITEM, None
    return DocumentBlockType.PARAGRAPH, None


def _heading_level(style_name: str) -> int:
    """«Heading 2» → 2; стиль без номера («Heading») считаем первым уровнем."""
    suffix = style_name[len(_HEADING_STYLE_PREFIX) :].strip()
    return int(suffix) if suffix.isdigit() else 1


def _is_list_item(paragraph: Paragraph, style_name: str) -> bool:
    # Word оформляет список либо стилем «List …», либо нумерацией numPr поверх обычного стиля.
    if style_name.startswith(_LIST_STYLE_PREFIX):
        return True
    p_pr = paragraph._p.pPr
    return p_pr is not None and p_pr.numPr is not None
