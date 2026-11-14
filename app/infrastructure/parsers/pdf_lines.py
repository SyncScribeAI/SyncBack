"""Чтение строк страницы PDF через PDFium.

Порядок символов в PDF задаёт поток содержимого, а не чтение: LibreOffice, например,
пишет цифры и знаки препинания отдельными текстовыми объектами, и PDFium отдаёт
«составляет МБ превышение 50 ,». Поэтому строки собираются заново по координатам:
символы группируются по базовой линии и сортируются слева направо, пробелы и границы
ячеек ставятся по горизонтальным разрывам. Пробелы самого файла и сгенерированные
PDFium отбрасываются: они стоят там, где их поставил поток, а не чтение.
"""

import ctypes
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from app.domain.exceptions import (
    DocumentParseError,
    PdfEncryptedError,
    PdfTooLargeError,
)

# PDFium не потокобезопасен: одновременные вызовы из двух потоков роняют процесс.
# Замок общий для разбора, геометрии и проверки при загрузке.
PDFIUM_LOCK = threading.RLock()

# Пороги в долях кегля [гипотеза]; подобраны на наборе P0 (docs/pdf-spike.md).
# Меняются только вместе с PDF_PARSER_VERSION: от них зависят границы и ref блоков.
_BASELINE_TOLERANCE_EM = 0.35
_WORD_GAP_EM = 0.12
_CELL_GAP_EM = 1.0
# Символ, который лёг на предыдущий почти в той же точке, рисует псевдожирный шрифт.
_DUPLICATE_SHIFT_EM = 0.1
# Мягкий перенос в конце строки убирается при склейке; обычный дефис остаётся.
_SOFT_HYPHEN = "\u00ad"
# Маркеры списков из шрифта Symbol приходят символами частной области Unicode.
_GLYPH_REPLACEMENTS = {"\uf0b7": "•", "\uf0a7": "▪", "\uf0d8": "➢", "\uf076": "❖"}

_LIST_MARKER = re.compile(r"[•▪➢❖◦·‣\-–*]|\d{1,3}[.)]|[a-zа-яё][.)]")

_FPDF_ERR_PASSWORD = 4


@dataclass(slots=True)
class PdfLine:
    """Строка страницы в порядке чтения.

    cells: (левая граница, начало, конец) кусков текста, разделённых широким разрывом.
    runs: (начало в text, индекс символа PDFium, длина) отрезков, где текст идёт подряд
    по символам страницы; вставленные пробелы входят в отрезок, только если в файле на
    их месте стоял ровно один символ.
    """

    page: int
    text: str
    left: float
    bottom: float
    right: float
    top: float
    baseline: float
    size: float
    cells: list[tuple[float, int, int]]
    runs: list[tuple[int, int, int]]
    hyphenated: bool
    # Ширина первого слова: по ней видно, поместилось бы оно в конец прошлой строки.
    first_word_width: float


@dataclass(slots=True)
class PdfPageLines:
    number: int
    width: float
    height: float
    lines: list[PdfLine]


@contextmanager
def open_pdf(source: bytes | IO[bytes], max_pages: int) -> Iterator[pdfium.PdfDocument]:
    """Открыть PDF под замком PDFium с проверкой шифрования и числа страниц."""
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(source)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == _FPDF_ERR_PASSWORD:
                raise PdfEncryptedError(
                    "PDF защищён паролем. Загрузите файл без пароля или исходный DOCX"
                ) from exc
            raise DocumentParseError(
                "Файл не читается как PDF. Загрузите исправный PDF или исходный DOCX"
            ) from exc
        try:
            if len(document) > max_pages:
                raise PdfTooLargeError(
                    f"В PDF {len(document)} страниц при допустимых {max_pages}. "
                    "Разделите файл или загрузите исходный DOCX"
                )
            yield document
        finally:
            document.close()


def iter_pages(document: pdfium.PdfDocument) -> Iterator[PdfPageLines]:
    """Страницы по одной: каждая закрывается сразу после чтения, в памяти одна страница.

    Вызывать внутри open_pdf, то есть под замком.
    """
    for index in range(len(document)):
        page = document[index]
        try:
            width, height = page.get_size()
            textpage = page.get_textpage()
            try:
                lines = _read_lines(textpage, index + 1)
            finally:
                textpage.close()
        finally:
            page.close()
        yield PdfPageLines(index + 1, width, height, lines)


def count_page_glyphs(document: pdfium.PdfDocument, index: int) -> int:
    """Число видимых символов страницы без сборки строк; для проверки при загрузке."""
    page = document[index]
    try:
        textpage = page.get_textpage()
        try:
            count = 0
            for i in range(pdfium_c.FPDFText_CountChars(textpage)):
                code = pdfium_c.FPDFText_GetUnicode(textpage, i)
                if (
                    code
                    and not chr(code).isspace()
                    and not pdfium_c.FPDFText_IsGenerated(textpage, i)
                ):
                    count += 1
            return count
        finally:
            textpage.close()
    finally:
        page.close()


# (baseline, left, right, bottom, top, size, char_index, char, hyphen)
_Glyph = tuple[float, float, float, float, float, float, int, str, bool]


def _read_lines(textpage: pdfium.PdfTextPage, page_number: int) -> list[PdfLine]:
    glyphs = list(_iter_glyphs(textpage))
    # Сверху вниз: координата y в PDF растёт вверх.
    glyphs.sort(key=lambda g: (-g[0], g[1]))
    lines: list[PdfLine] = []
    current: list[_Glyph] = []
    for glyph in glyphs:
        if current:
            first = current[0]
            tolerance = _BASELINE_TOLERANCE_EM * max(first[5], glyph[5], 1.0)
            if first[0] - glyph[0] > tolerance:
                lines.append(_build_line(current, page_number))
                current = []
        current.append(glyph)
    if current:
        lines.append(_build_line(current, page_number))
    return lines


def _iter_glyphs(textpage: pdfium.PdfTextPage) -> Iterator[_Glyph]:
    rect = pdfium_c.FS_RECTF()
    origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
    for i in range(pdfium_c.FPDFText_CountChars(textpage)):
        code = pdfium_c.FPDFText_GetUnicode(textpage, i)
        if not code or pdfium_c.FPDFText_IsGenerated(textpage, i):
            continue
        char = chr(code)
        if char.isspace():
            continue
        if not pdfium_c.FPDFText_GetLooseCharBox(textpage, i, ctypes.byref(rect)):
            continue
        pdfium_c.FPDFText_GetCharOrigin(textpage, i, ctypes.byref(origin_x), ctypes.byref(origin_y))
        yield (
            origin_y.value,
            rect.left,
            rect.right,
            rect.bottom,
            rect.top,
            pdfium_c.FPDFText_GetFontSize(textpage, i),
            i,
            _GLYPH_REPLACEMENTS.get(char, char),
            bool(pdfium_c.FPDFText_IsHyphen(textpage, i)),
        )


def _build_line(glyphs: list[_Glyph], page_number: int) -> PdfLine:
    glyphs.sort(key=lambda g: g[1])
    chars: list[str] = []
    cells: list[tuple[float, int, int]] = []
    runs: list[tuple[int, int, int]] = []
    cell_left, cell_start = glyphs[0][1], 0
    prev: _Glyph | None = None
    sizes: list[float] = []
    first_word_right: float | None = None
    for glyph in glyphs:
        _, left, right, _, _, size, char_index, char, _ = glyph
        if prev is not None:
            em = max(prev[5], size, 1.0)
            if char == prev[7] and abs(left - prev[1]) < _DUPLICATE_SHIFT_EM * em:
                continue
            gap = left - prev[2]
            # Отступ после маркера списка широк, но маркер не отдельная ячейка.
            if gap > _CELL_GAP_EM * em and not _LIST_MARKER.fullmatch("".join(chars[cell_start:])):
                cells.append((cell_left, cell_start, len(chars)))
                first_word_right = first_word_right or prev[2]
                chars.append(" ")
                cell_left, cell_start = left, len(chars)
            elif gap > _WORD_GAP_EM * em:
                first_word_right = first_word_right or prev[2]
                chars.append(" ")
        offset = len(chars)
        chars.append(char)
        sizes.append(size)
        _extend_runs(runs, offset, char_index)
        prev = glyph
    cells.append((cell_left, cell_start, len(chars)))
    sizes.sort()
    last = glyphs[-1]
    return PdfLine(
        page=page_number,
        text="".join(chars),
        left=glyphs[0][1],
        bottom=min(g[3] for g in glyphs),
        right=max(g[2] for g in glyphs),
        top=max(g[4] for g in glyphs),
        baseline=glyphs[0][0],
        size=sizes[len(sizes) // 2],
        cells=cells,
        runs=runs,
        hyphenated=len(glyphs) > 1 and (last[8] or last[7] == _SOFT_HYPHEN),
        first_word_width=(first_word_right or last[2]) - glyphs[0][1],
    )


def _extend_runs(runs: list[tuple[int, int, int]], offset: int, char_index: int) -> None:
    """Продлить последний отрезок, если символ идёт за ним и в тексте, и в файле."""
    if runs:
        start, first_index, count = runs[-1]
        gap_in_text = offset - (start + count)
        gap_in_file = char_index - (first_index + count)
        if gap_in_text == gap_in_file and gap_in_text in (0, 1):
            runs[-1] = (start, first_index, offset - start + 1)
            return
    runs.append((offset, char_index, 1))
