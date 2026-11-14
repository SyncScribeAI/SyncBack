"""Единый порядок абзацев .docx для парсера и экспортёра.

Парсер строит plain_text, а экспортёр ищет в нём old_text; если они обходят документ
по-разному, принятая правка в ячейке таблицы молча не попадёт в файл. Поэтому оба
идут через этот обход: абзацы тела и ячеек в порядке документа.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph


@dataclass(frozen=True, slots=True)
class DocxParagraph:
    paragraph: Paragraph
    in_table: bool


def iter_docx_paragraphs(document: Any) -> Iterator[DocxParagraph]:
    """O(P): каждый абзац выдаётся ровно один раз, включая вложенные таблицы."""
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            yield DocxParagraph(item, in_table=False)
        else:
            yield from _iter_table(item)


def _iter_table(table: Table) -> Iterator[DocxParagraph]:
    for row in table.rows:
        # Физические w:tc, а не row.cells: row.cells повторяет объединённую ячейку
        # на каждую колонку, и её текст задвоился бы в plain_text.
        for tc in row._tr.tc_lst:
            for item in _Cell(tc, table).iter_inner_content():
                if isinstance(item, Paragraph):
                    yield DocxParagraph(item, in_table=True)
                else:
                    yield from _iter_table(item)


def iter_docx_body_items(document: Any) -> Iterator[Paragraph | Table]:
    """Элементы тела в порядке документа: абзацы и таблицы верхнего уровня."""
    yield from document.iter_inner_content()


def iter_table_paragraphs(table: Table) -> Iterator[DocxParagraph]:
    """Абзацы таблицы в том же порядке, в каком их выдаёт iter_docx_paragraphs."""
    return _iter_table(table)


def table_rows(table: Table) -> list[list[str]]:
    """Таблица строками ячеек: для источника важна связь «ячейка ↔ строка» («Цена | 100 руб.»)."""
    return [_row_cells(row._tr.tc_lst, table) for row in table.rows]


def _row_cells(tcs: list[Any], table: Table) -> list[str]:
    cells: list[str] = []
    for tc in tcs:
        # Абзацы ячейки и вложенных таблиц сливаются в одну строку: строка таблицы
        # в источнике должна оставаться одной строкой разметки.
        parts: list[str] = []
        for item in _Cell(tc, table).iter_inner_content():
            if isinstance(item, Paragraph):
                parts.append(item.text)
            else:
                parts.extend(p.paragraph.text for p in _iter_table(item))
        cells.append(" ".join(" ".join(parts).split()))
    return cells
