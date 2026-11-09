import io

from docx import Document as DocxDocument
from docx.text.paragraph import Paragraph

from app.domain.document_blocks import DocumentBlocksBuilder
from app.domain.interfaces.document_parser import (
    DocumentBlockType,
    DocumentSection,
    ParsedDocument,
)
from app.domain.text_normalization import normalize_text
from app.infrastructure.docx_paragraphs import iter_docx_paragraphs

_HEADING_STYLE_PREFIX = "Heading"
_LIST_STYLE_PREFIX = "List"


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
            style_name = paragraph.style.name if paragraph.style is not None else ""
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
                level: int | None = None
                if item.in_table:
                    block_type = DocumentBlockType.TABLE
                elif is_heading:
                    block_type = DocumentBlockType.HEADING
                    level = _heading_level(style_name)
                elif _is_list_item(paragraph, style_name):
                    block_type = DocumentBlockType.LIST_ITEM
                else:
                    block_type = DocumentBlockType.PARAGRAPH
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

    def extract_text(self, raw_bytes: bytes) -> str:
        docx_document = DocxDocument(io.BytesIO(raw_bytes))
        return "\n".join(
            normalize_text(item.paragraph.text) for item in iter_docx_paragraphs(docx_document)
        )


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
