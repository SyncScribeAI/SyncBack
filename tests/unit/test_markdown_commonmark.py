"""Разбор Markdown по CommonMark и GFM: типы, границы и совместимость с прежним разбором."""

from __future__ import annotations

import gc
import time

import pytest

from app.domain.interfaces.document_parser import DocumentBlockType, ParsedDocument
from app.domain.text_normalization import normalize_text
from app.infrastructure.parsers.markdown_parser import MarkdownParser
from tests.unit._legacy_markdown import legacy_split_blocks

H = DocumentBlockType.HEADING
P = DocumentBlockType.PARAGRAPH
LI = DocumentBlockType.LIST_ITEM
C = DocumentBlockType.CODE
T = DocumentBlockType.TABLE
O = DocumentBlockType.OTHER


def _parse(raw: str) -> ParsedDocument:
    return MarkdownParser().parse(raw.encode())


def _summary(parsed: ParsedDocument) -> list[tuple[DocumentBlockType, str, bool]]:
    return [(b.type, b.text_in(parsed.plain_text), b.editable) for b in parsed.blocks]


def _assert_no_overlaps(parsed: ParsedDocument) -> None:
    previous_end = 0
    for block in parsed.blocks:
        assert previous_end <= block.start_offset < block.end_offset
        previous_end = block.end_offset
    assert previous_end <= len(parsed.plain_text)


def test_front_matter_is_one_read_only_block() -> None:
    parsed = _parse("---\ntitle: Спека\ntags: [a]\n---\n# Заголовок\n\nТекст\n")
    assert _summary(parsed) == [
        (O, "---\ntitle: Спека\ntags: [a]\n---", False),
        (H, "# Заголовок", False),
        (P, "Текст", True),
    ]
    assert [s.ref for s in parsed.sections] == ["Заголовок"]


def test_front_matter_closed_by_dots() -> None:
    parsed = _parse("---\na: 1\n...\nТекст\n")
    assert _summary(parsed)[0] == (O, "---\na: 1\n...", False)


def test_unclosed_front_matter_is_regular_markdown() -> None:
    parsed = _parse("---\nТекст\n")
    assert [b.type for b in parsed.blocks] == [O, P]


def test_setext_headings_and_section_titles() -> None:
    parsed = _parse("Тарифы\n======\n\nЦена\n\nСроки\n-----\nТекст\n")
    assert _summary(parsed) == [
        (H, "Тарифы\n======", False),
        (P, "Цена", True),
        (H, "Сроки\n-----", False),
        (P, "Текст", True),
    ]
    assert [b.heading_level for b in parsed.blocks if b.type is H] == [1, 2]
    assert [s.ref for s in parsed.sections] == ["Тарифы", "Сроки"]


def test_blockquote_is_expanded_into_nested_blocks() -> None:
    parsed = _parse("> Первый абзац\n>\n> - пункт\n\nПосле\n")
    assert _summary(parsed) == [
        (P, "> Первый абзац", True),
        (LI, "> - пункт", True),
        (P, "После", True),
    ]


def test_html_block_and_hr_are_read_only() -> None:
    parsed = _parse("<div align=center>\n<b>x</b>\n</div>\n\n***\n\nТекст\n")
    assert _summary(parsed) == [
        (O, "<div align=center>\n<b>x</b>\n</div>", False),
        (O, "***", False),
        (P, "Текст", True),
    ]


def test_indented_code_is_code() -> None:
    parsed = _parse("Текст\n\n    x = 1\n    y = 2\n\nПосле\n")
    assert _summary(parsed) == [
        (P, "Текст", True),
        (C, "x = 1\n    y = 2", False),
        (P, "После", True),
    ]


def test_table_without_leading_pipe() -> None:
    parsed = _parse("Формат | Лимит\n--- | ---\nPDF | 50 МБ\n\nТекст\n")
    assert _summary(parsed) == [
        (T, "Формат | Лимит\n--- | ---\nPDF | 50 МБ", False),
        (P, "Текст", True),
    ]


def test_nested_list_items_do_not_overlap_parent() -> None:
    raw = "- родитель\n  - ребёнок\n  - ещё\n\n  второй абзац родителя\n- сосед\n"
    parsed = _parse(raw)
    assert _summary(parsed) == [
        (LI, "- родитель", True),
        (LI, "- ребёнок", True),
        (LI, "- ещё", True),
        (LI, "второй абзац родителя", True),
        (LI, "- сосед", True),
    ]
    _assert_no_overlaps(parsed)


def test_list_item_marker_on_its_own_line_is_part_of_first_paragraph() -> None:
    parsed = _parse("1.\n   текст пункта\n")
    assert _summary(parsed) == [(LI, "1.\n   текст пункта", True)]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "\n\n",
        "---\n",
        "---\n---\n",
        "> \n",
        "- \n",
        "```\nне закрыт",
        "| a |\n",
        "Текст\n\n<!-- комментарий -->\n",
        "  # Отступ\r\n\u00a0\r\nТекст\u200b",
    ],
)
def test_blocks_stay_inside_text_and_do_not_overlap(raw: str) -> None:
    parsed = _parse(raw)
    assert parsed.plain_text == normalize_text(raw)
    _assert_no_overlaps(parsed)
    for block in parsed.blocks:
        text = block.text_in(parsed.plain_text)
        assert text and text == text.strip()


# Документы только из конструкций, которые прежний разбор понимал верно: на них блоки обоих
# разборов обязаны совпасть, иначе сохранённые block_id правок разойдутся.
COMPATIBLE = [
    "# Сервис\nПервая строка\nвторая строка\n\n## Роли\n- customer\n  продолжение\n* cook\n1. courier\n",
    "## Сроки\n\nСрок ответа — 10 рабочих дней.\n\n## Оплата\n\nОплата принимается картой.\n",
    "# Установка\n\n```bash\n# комментарий\npip install x\n```\n\n## Запуск\nrun\n",
    "Intro\n\n```\ncode\n\nmore code\n",
    "same\n\nsame\n\nsame\n",
    "# A\r\nx\u00a0y\r\n",
    "| a | b |\n|---|---|\n| 1 | 2 |\n\nПосле таблицы\n",
    "Абзац один.\n\nАбзац два\nс переносом.\n\n### Итог\n\n- a\n- b\n- c\n",
]


@pytest.mark.parametrize("raw", COMPATIBLE)
def test_blocks_match_legacy_parser_on_compatible_markup(raw: str) -> None:
    parsed = _parse(raw)
    legacy = legacy_split_blocks(parsed.plain_text)
    assert [(b.ref, b.start_offset, b.end_offset) for b in parsed.blocks] == [
        (b.ref, b.start_offset, b.end_offset) for b in legacy
    ]


def _parse_seconds(raw: str) -> float:
    started = time.perf_counter()
    blocks = len(_parse(raw).blocks)
    elapsed = time.perf_counter() - started
    assert blocks == 5 * raw.count("## Раздел")
    return elapsed


@pytest.mark.perf
def test_large_document_parses_linearly() -> None:
    unit = "## Раздел\n\nАбзац с **выделением** и `кодом`.\n\n- пункт\n- пункт\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
    copies = 40_000 // len(unit)
    # Абсолютное время зависит от машины и покрытия, поэтому сравнивается рост:
    # линейный разбор удваивает время, квадратичный учетверяет. Сборщик мусора
    # выключен, потому что его проходы по куче pytest искажают отношение, а минимум
    # из трёх замеров отсекает паузы соседних процессов.
    gc.collect()
    gc.disable()
    try:
        single = min(_parse_seconds(unit * copies) for _ in range(3))
        double = min(_parse_seconds(unit * 2 * copies) for _ in range(3))
    finally:
        gc.enable()
    assert double / single < 3.0
