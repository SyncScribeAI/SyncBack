"""Блоки документа: границы, типы и ref одинаково строятся для .docx, .md и .txt.

Главный инвариант — текст блока всегда равен plain_text[start:end]: на нём держатся
target_block_id и old_text в контракте ML 2.0.
"""

import io

import pytest
from docx import Document as DocxDocument

from app.domain.document_blocks import DocumentBlocksBuilder, block_ref
from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.interfaces.document_parser import DocumentBlockType, ParsedDocument
from app.infrastructure.exporters.docx_exporter import DocxExporter
from app.infrastructure.parsers.docx_parser import DocxParser
from app.infrastructure.parsers.markdown_parser import MarkdownParser
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.parsers.txt_parser import TxtParser

H = DocumentBlockType.HEADING
P = DocumentBlockType.PARAGRAPH
LI = DocumentBlockType.LIST_ITEM
C = DocumentBlockType.CODE
T = DocumentBlockType.TABLE
O = DocumentBlockType.OTHER


def _assert_invariants(parsed: ParsedDocument) -> None:
    previous_end = 0
    refs: set[str] = set()
    for position, block in enumerate(parsed.blocks):
        assert block.position == position
        assert previous_end <= block.start_offset < block.end_offset <= len(parsed.plain_text)
        text = block.text_in(parsed.plain_text)
        assert text == text.strip() and text
        assert block.ref not in refs
        refs.add(block.ref)
        previous_end = block.end_offset


def _summary(parsed: ParsedDocument) -> list[tuple[DocumentBlockType, str]]:
    return [(b.type, b.text_in(parsed.plain_text)) for b in parsed.blocks]


def _docx_bytes(*paragraphs: tuple[str, str | None]) -> bytes:
    document = DocxDocument()
    for text, style in paragraphs:
        document.add_paragraph(text, style=style)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- markdown ---------------------------------------------------------------


def test_markdown_headings_paragraphs_lists_and_code() -> None:
    raw = (
        "# Сервис\n"
        "Первая строка\nвторая строка\n"
        "\n"
        "## Роли\n"
        "- customer\n"
        "  продолжение пункта\n"
        "* cook\n"
        "1. courier\n"
        "\n"
        "```python\n"
        "x = 1\n"
        "\n"
        "# не заголовок\n"
        "```\n"
        "---\n"
        "| a | b |\n"
        "|---|---|\n"
        "Итог\n"
    )
    parsed = MarkdownParser().parse(raw.encode())
    _assert_invariants(parsed)
    assert _summary(parsed) == [
        (H, "# Сервис"),
        (P, "Первая строка\nвторая строка"),
        (H, "## Роли"),
        (LI, "- customer\n  продолжение пункта"),
        (LI, "* cook"),
        (LI, "1. courier"),
        (C, "```python\nx = 1\n\n# не заголовок\n```"),
        (O, "---"),
        # По GFM строка сразу после таблицы без пустой строки — её строка.
        (T, "| a | b |\n|---|---|\nИтог"),
    ]
    headings = [b for b in parsed.blocks if b.type is H]
    assert [b.heading_level for b in headings] == [1, 2]
    assert [b.ref for b in parsed.blocks[:3]] == ["h-0", "p-1", "h-2"]
    assert {b.type for b in parsed.blocks if not b.editable} == {H, C, T, O}


def test_markdown_section_starts_match_heading_blocks() -> None:
    parsed = MarkdownParser().parse("# A\ntext\n\n## B\nmore\n".encode())
    heading_starts = [b.start_offset for b in parsed.blocks if b.type is H]
    assert [s.start_offset for s in parsed.sections] == heading_starts


def test_markdown_unclosed_fence_runs_to_end() -> None:
    parsed = MarkdownParser().parse(b"Intro\n\n```\ncode\n\nmore code\n")
    _assert_invariants(parsed)
    assert _summary(parsed) == [(P, "Intro"), (C, "```\ncode\n\nmore code")]


def test_markdown_repeated_lines_get_distinct_offsets() -> None:
    parsed = MarkdownParser().parse(b"same\n\nsame\n\nsame\n")
    _assert_invariants(parsed)
    assert [b.start_offset for b in parsed.blocks] == [0, 6, 12]


def test_markdown_crlf_and_nbsp_are_normalized_before_offsets() -> None:
    parsed = MarkdownParser().parse("# A\r\nx\u00a0y\r\n".encode())
    _assert_invariants(parsed)
    assert _summary(parsed) == [(H, "# A"), (P, "x y")]


# --- txt --------------------------------------------------------------------


def test_txt_splits_by_blank_lines() -> None:
    parsed = TxtParser().parse(b"  first\nline\n\n \t\nsecond  \n\n\nthird")
    _assert_invariants(parsed)
    assert _summary(parsed) == [(P, "first\nline"), (P, "second"), (P, "third")]
    assert all(b.editable for b in parsed.blocks)


def test_txt_without_blank_lines_splits_by_lines() -> None:
    parsed = TxtParser().parse(b"Alpha\nBeta\n")
    _assert_invariants(parsed)
    assert _summary(parsed) == [(P, "Alpha"), (P, "Beta")]
    assert [b.ref for b in parsed.blocks] == ["p-0", "p-1"]


@pytest.mark.parametrize("raw", [b"", b"\n\n", b"   "])
def test_txt_empty_document_has_no_blocks(raw: bytes) -> None:
    assert TxtParser().parse(raw).blocks == []


# --- docx -------------------------------------------------------------------


def test_docx_blocks_follow_paragraph_styles() -> None:
    raw = _docx_bytes(
        ("Auth Service", "Heading 1"),
        ("  Описание  ", None),
        ("", None),
        ("Роли", "Heading 2"),
        ("customer", "List Bullet"),
        ("cook", "List Number"),
    )
    parsed = DocxParser().parse(raw)
    _assert_invariants(parsed)
    assert _summary(parsed) == [
        (H, "Auth Service"),
        (P, "Описание"),
        (H, "Роли"),
        (LI, "customer"),
        (LI, "cook"),
    ]
    assert [b.heading_level for b in parsed.blocks] == [1, None, 2, None, None]
    # Пустой абзац остаётся в plain_text, но блока не даёт, поэтому позиция «Роли» — 2.
    assert parsed.blocks[2].ref == "h-2"
    assert [s.ref for s in parsed.sections] == ["Auth Service", "Роли"]


# --- сборщик ----------------------------------------------------------------


def test_builder_rejects_overlapping_and_out_of_range_blocks() -> None:
    builder = DocumentBlocksBuilder(text_length=10)
    builder.add(P, 0, 5)
    with pytest.raises(ValueError):
        builder.add(P, 3, 8)
    with pytest.raises(ValueError):
        builder.add(P, 6, 11)
    builder.add(P, 7, 7)
    assert len(builder.build()) == 1


def test_heading_level_is_dropped_for_non_heading_blocks() -> None:
    builder = DocumentBlocksBuilder(text_length=4)
    builder.add(P, 0, 4, heading_level=2)
    assert builder.build()[0].heading_level is None


def test_block_ref_prefixes() -> None:
    assert [block_ref(t, 3) for t in DocumentBlockType] == [
        "h-3",
        "p-3",
        "li-3",
        "c-3",
        "t-3",
        "o-3",
    ]


# --- таблицы .docx ------------------------------------------------------------


def _docx_with_table() -> bytes:
    """Абзац, таблица 2×2 с объединённой шапкой и вложенной таблицей, абзац."""
    document = DocxDocument()
    document.add_paragraph("До таблицы")
    table = document.add_table(rows=2, cols=2)
    header = table.cell(0, 0).merge(table.cell(0, 1))
    header.text = "Тарифы"
    table.cell(1, 0).text = "Цена"
    price = table.cell(1, 1)
    price.text = "100 руб."
    nested = price.add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "НДС включён"
    document.add_paragraph("После таблицы")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_tables_are_read_in_document_order_without_duplicates() -> None:
    parsed = DocxParser().parse(_docx_with_table())
    _assert_invariants(parsed)
    assert _summary(parsed) == [
        (P, "До таблицы"),
        (T, "Тарифы"),
        (T, "Цена"),
        (T, "100 руб."),
        (T, "НДС включён"),
        (P, "После таблицы"),
    ]
    # Объединённая шапка — одна физическая ячейка, её текст не задваивается.
    assert parsed.plain_text.count("Тарифы") == 1
    assert [b.editable for b in parsed.blocks] == [True, False, False, False, False, True]


def test_docx_heading_style_inside_table_is_not_a_section() -> None:
    document = DocxDocument()
    document.add_heading("Раздел", level=1)
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.paragraphs[0].text = "Шапка ячейки"
    cell.paragraphs[0].style = document.styles["Heading 2"]
    buffer = io.BytesIO()
    document.save(buffer)
    parsed = DocxParser().parse(buffer.getvalue())
    assert [s.ref for s in parsed.sections] == ["Раздел"]
    assert _summary(parsed) == [(H, "Раздел"), (T, "Шапка ячейки")]


def test_docx_export_applies_change_inside_table_cell() -> None:
    change = AppliedChange(
        section_ref="", change_type="modify", old_text="100 руб.", new_text="120 руб."
    )
    exported = DocxExporter().apply_changes(_docx_with_table(), [change])
    text = DocxParser().extract_text(exported)
    assert "120 руб." in text
    assert "100 руб." not in text


# --- разделы markdown из заголовков -------------------------------------------


def test_markdown_sections_ignore_hashes_inside_code() -> None:
    raw = "# Установка\n\n```bash\n# комментарий\npip install x\n```\n\n## Запуск\nrun\n"
    parsed = MarkdownParser().parse(raw.encode())
    assert [(s.ref, s.start_offset) for s in parsed.sections] == [
        ("Установка", 0),
        ("Запуск", raw.index("## Запуск")),
    ]
    assert parsed.sections[0].end_offset == parsed.sections[1].start_offset
    assert parsed.sections[-1].end_offset == len(parsed.plain_text)


def test_markdown_heading_does_not_swallow_next_line() -> None:
    parsed = MarkdownParser().parse(b"#\ntext\n")
    # «#» без текста по CommonMark — пустой заголовок: служебный блок, не раздел.
    assert parsed.sections == []
    assert _summary(parsed) == [(O, "#"), (P, "text")]
    assert parsed.blocks[0].editable is False


# --- extract_text ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "raw"),
    [
        ("spec.txt", "Alpha\r\n\r\nBeta\u00a0x".encode()),
        ("spec.md", "# A\n```\n# b\n```\nтекст\u00a0x\n".encode()),
        ("spec.docx", _docx_with_table()),
    ],
)
def test_extract_text_matches_parse_plain_text(filename: str, raw: bytes) -> None:
    registry = DocumentParserRegistry()
    assert (
        registry.extract_text(filename, raw) == registry.parse_by_filename(filename, raw).plain_text
    )
