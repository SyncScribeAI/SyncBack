"""Экспорт PDF: принятые правки ставятся аннотациями рецензирования поверх исходных страниц.

Текст PDF не переписывается: спайк P0 показал, что замена текста на месте портит глифы,
когда в подмножестве шрифта нет нужных символов, и этого не видно по повторному
извлечению. Поэтому замена и удаление зачёркивают старый текст (/StrikeOut), вставка
ставит знак вставки (/Caret), а новый текст лежит в /Contents и виден во всплывающей
заметке просмотрщика.

Файл дописывается инкрементально: исходные байты остаются началом результата, и подпись
или контрольная сумма исходника по-прежнему проверяется на этой части.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from app.domain.document_geometry import PageRect
from app.domain.interfaces.document_exporter import AppliedChange

DEFAULT_ANNOTATION_AUTHOR = "SyncScribe"

# Цвет отметок рецензирования, привычный по Word и Acrobat.
_COLOR = (0.86, 0.1, 0.1)
# /F 4 (Print): отметка печатается вместе со страницей, как и правка в Word.
_PRINT_FLAG = 4
_DELETE_NOTE = "Удалить"
_CARET_MIN_SIZE = 9.0
# Отметки правок без положения на странице складываются столбиком в углу первой страницы.
_NOTE_SIZE = 18.0
_NOTE_STEP = 22.0


@dataclass(frozen=True, slots=True)
class _Mark:
    """Одна аннотация: часть правки на одной странице."""

    page: int
    rects: tuple[PageRect, ...]


class PdfAnnotationExporter:
    def __init__(self, author: str = DEFAULT_ANNOTATION_AUTHOR) -> None:
        self._author = author

    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes:
        """Исходный PDF с аннотацией на каждую принятую правку.

        Правка на нескольких страницах даёт по аннотации на страницу; продолжения
        связаны с первой через /IRT и /RT /Group, поэтому просмотрщик показывает их
        одной заметкой. Правка без прямоугольников (сбой геометрии при анализе) ставится
        заметкой на первой странице, чтобы принятая правка не пропала из файла.
        """
        if not changes:
            return raw_bytes
        writer = PdfWriter(PdfReader(io.BytesIO(raw_bytes)), incremental=True)
        page_count = len(writer.pages)
        stamp = TextStringObject(datetime.now(UTC).strftime("D:%Y%m%d%H%M%SZ"))
        unplaced = 0
        for index, change in enumerate(changes):
            marks = list(_marks(change.rects, page_count))
            if not marks:
                self._add_note(writer, change, index, unplaced, stamp)
                unplaced += 1
                continue
            first: IndirectObject | None = None
            for mark in marks:
                annotation = self._markup(change, mark, index, stamp, writer)
                if first is not None:
                    annotation[NameObject("/IRT")] = first
                    annotation[NameObject("/RT")] = NameObject("/Group")
                added = writer.add_annotation(mark.page - 1, annotation)
                if first is None:
                    first = added.indirect_reference
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    def _markup(
        self,
        change: AppliedChange,
        mark: _Mark,
        index: int,
        stamp: TextStringObject,
        writer: PdfWriter,
    ) -> DictionaryObject:
        if change.change_type == "add":
            # Ориентир вставки: символ перед ней, а в начале документа первый символ
            # (app.domain.document_geometry.highlight_span).
            at_start = change.start_offset == 0
            anchor = mark.rects[0] if at_start else mark.rects[-1]
            return self._caret(anchor, at_start, change.new_text or "", index, stamp, writer)
        contents = change.new_text if change.change_type == "modify" else None
        return self._strike(mark.rects, contents or _DELETE_NOTE, index, stamp, writer)

    def _strike(
        self,
        rects: tuple[PageRect, ...],
        contents: str,
        index: int,
        stamp: TextStringObject,
        writer: PdfWriter,
    ) -> DictionaryObject:
        box = _union(rects)
        quads: list[float] = []
        lines: list[str] = []
        for r in rects:
            # Порядок вершин как у Acrobat: верх слева, верх справа, низ слева, низ справа.
            quads += [r.x0, r.y1, r.x1, r.y1, r.x0, r.y0, r.x1, r.y0]
            mid = (r.y0 + r.y1) / 2
            width = max(0.8, (r.y1 - r.y0) * 0.1)
            lines.append(f"{width:.2f} w {r.x0:.2f} {mid:.2f} m {r.x1:.2f} {mid:.2f} l S")
        annotation = self._base("/StrikeOut", box, contents, index, stamp)
        annotation[NameObject("/QuadPoints")] = _numbers(quads)
        appearance = f"{_rgb('RG')} " + " ".join(lines)
        annotation[NameObject("/AP")] = _appearance(writer, box, appearance)
        return annotation

    def _caret(
        self,
        anchor: PageRect,
        at_start: bool,
        contents: str,
        index: int,
        stamp: TextStringObject,
        writer: PdfWriter,
    ) -> DictionaryObject:
        # Ориентиром бывает точка или запятая высотой в пару пунктов, а знак вставки
        # должен быть заметен, поэтому у него есть нижняя граница размера.
        size = max(anchor.y1 - anchor.y0, _CARET_MIN_SIZE)
        x = anchor.x0 if at_start else anchor.x1
        box = PageRect(
            anchor.page,
            x - size * 0.3,
            anchor.y0 - size * 0.25,
            x + size * 0.3,
            anchor.y0 + size * 0.3,
        )
        annotation = self._base("/Caret", box, contents, index, stamp)
        triangle = (
            f"{_rgb('rg')} {box.x0:.2f} {box.y0:.2f} m {box.x1:.2f} {box.y0:.2f} l "
            f"{x:.2f} {box.y1:.2f} l f"
        )
        annotation[NameObject("/AP")] = _appearance(writer, box, triangle)
        return annotation

    def _add_note(
        self,
        writer: PdfWriter,
        change: AppliedChange,
        index: int,
        slot: int,
        stamp: TextStringObject,
    ) -> None:
        page = writer.pages[0]
        crop = page.cropbox
        left = float(crop.left) + 6
        top = float(crop.top) - 6 - slot * _NOTE_STEP
        box = PageRect(1, left, top - _NOTE_SIZE, left + _NOTE_SIZE, top)
        contents = f"Место правки не найдено на странице. {_describe(change)}"
        annotation = self._base("/Text", box, contents, index, stamp)
        annotation[NameObject("/Name")] = NameObject("/Comment")
        square = (
            f"1 1 0.6 rg {_rgb('RG')} 1 w {box.x0 + 0.5:.2f} {box.y0 + 0.5:.2f} "
            f"{_NOTE_SIZE - 1:.2f} {_NOTE_SIZE - 1:.2f} re B"
        )
        annotation[NameObject("/AP")] = _appearance(writer, box, square)
        writer.add_annotation(0, annotation)

    def _base(
        self, subtype: str, box: PageRect, contents: str, index: int, stamp: TextStringObject
    ) -> DictionaryObject:
        return DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject(subtype),
                NameObject("/Rect"): _numbers([box.x0, box.y0, box.x1, box.y1]),
                NameObject("/Contents"): TextStringObject(contents),
                NameObject("/T"): TextStringObject(self._author),
                NameObject("/NM"): TextStringObject(f"syncscribe-{index}"),
                NameObject("/M"): stamp,
                NameObject("/C"): _numbers(list(_COLOR)),
                NameObject("/F"): NumberObject(_PRINT_FLAG),
            }
        )


def _describe(change: AppliedChange) -> str:
    old, new = change.old_text or "", change.new_text or ""
    if change.change_type == "add":
        return f"Вставить: «{new}»."
    if change.change_type == "delete":
        return f"Удалить: «{old}»."
    return f"Было: «{old}». Стало: «{new}»."


def _marks(rects: tuple[PageRect, ...], page_count: int) -> Iterator[_Mark]:
    """Прямоугольники правки по страницам, в порядке текста.

    Номер страницы вне файла пропускается: геометрия могла остаться от другой версии
    файла, а аннотация на несуществующей странице сломала бы запись.
    """
    valid = (r for r in rects if 1 <= r.page <= page_count)
    for page, group in groupby(valid, key=lambda r: r.page):
        yield _Mark(page, tuple(group))


def _union(rects: tuple[PageRect, ...]) -> PageRect:
    return PageRect(
        rects[0].page,
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )


def _numbers(values: list[float]) -> ArrayObject:
    return ArrayObject(FloatObject(round(v, 2)) for v in values)


def _rgb(operator: str) -> str:
    return f"{_COLOR[0]} {_COLOR[1]} {_COLOR[2]} {operator}"


def _appearance(writer: PdfWriter, box: PageRect, content: str) -> DictionaryObject:
    """Поток внешнего вида /AP /N.

    Просмотрщик без него может построить вид сам, а может не нарисовать отметку вовсе;
    Acrobat, pdf.js и Okular на наборе P0 не проверены, поэтому вид задаётся явно.
    BBox совпадает с /Rect, матрица единичная, поэтому рисунок задаётся прямо в
    координатах страницы.
    """
    stream = DecodedStreamObject()
    stream.set_data(content.encode("ascii"))
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): _numbers([box.x0, box.y0, box.x1, box.y1]),
        }
    )
    # Поток в PDF бывает только косвенным объектом, публичного метода для этого в pypdf нет.
    reference = writer._add_object(stream)
    return DictionaryObject({NameObject("/N"): reference})
