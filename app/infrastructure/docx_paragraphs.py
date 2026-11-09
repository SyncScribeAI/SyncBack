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
