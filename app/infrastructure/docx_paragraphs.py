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


def iter_docx_body(document: Any) -> Iterator[Paragraph | list[list[str]]]:
    """Тело документа для источника: абзацы как есть, таблица — строками ячеек.

    Для источника важна связь «ячейка ↔ строка» («Цена | 100 руб.»), поэтому таблица
    отдаётся целиком, а не абзацами ячеек, как в iter_docx_paragraphs. O(P).
    """
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            yield item
        else:
            yield [_row_cells(row._tr.tc_lst, item) for row in item.rows]


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
