"""Сборка блоков документа из строк страниц PDF.

В PDF нет абзацев, заголовков и таблиц, есть только строки с координатами, поэтому
структура восстанавливается эвристиками. Пороги являются гипотезой: они подобраны на
наборе P0 (docs/pdf-spike.md) и меняются только вместе с PDF_PARSER_VERSION, потому что
от них зависят границы блоков и их ref.

Каждый блок хранит куски строк, а не готовый текст: из одних и тех же кусков
собираются plain_text документа, карта к символам PDFium и текст источника, и все
три сходятся без повторного сопоставления. Сложность O(число символов) плюс
сортировка колонок таблицы; в памяти текст, строки и карта.
"""

import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.domain.document_blocks import DocumentBlocksBuilder, heading_title
from app.domain.document_geometry import TextRuns
from app.domain.interfaces.document_parser import (
    DocumentBlockType,
    DocumentSection,
    ParsedDocument,
)
from app.domain.source_text import FragmentPart, SourceFragment
from app.infrastructure.parsers.pdf_lines import PdfLine, PdfPageLines

# Меняется при любой правке эвристик: по ней кэш разбора отличает старые блоки от новых.
PDF_PARSER_VERSION = 1
# Вид локатора источника: прямоугольники строк фрагмента на страницах.
PDF_RECTS_LOCATOR = "pdf_rects"

# [гипотеза] Заголовок: короткая строка с кеглем не меньше 1.15 медианного по документу.
_HEADING_SIZE_RATIO = 1.15
_HEADING_MAX_CHARS = 150
_HEADING_MAX_WIDTH_SHARE = 0.8
# [гипотеза] Новый абзац: шаг базовой линии больше обычного межстрочного в 1.3 раза.
_PARAGRAPH_GAP_RATIO = 1.3
# [гипотеза] Пробел перед словом, которое поместилось бы в прошлую строку, в долях кегля.
_WORD_FIT_SPACE_EM = 0.5
_SIZE_CHANGE_RATIO = 0.1
# [гипотеза] Колонтитул: строка с тем же текстом (цифры не различаются) на той же
# высоте у верхнего или нижнего края больше чем на половине страниц.
_REPEATED_MIN_PAGE_SHARE = 0.5
_REPEATED_EDGE_SHARE = 0.12
_REPEATED_BASELINE_STEP = 3.0
_REPEATED_EDGE_LINES = 2
_PAGE_NUMBER_MAX_LETTERS = 15
# [гипотеза] Левые границы ячеек совпадают с точностью до 3 пт.
_COLUMN_TOLERANCE = 3.0
_MIN_TABLE_COLUMNS = 2

_LIST_MARKER = re.compile(r"^(?:[•▪➢❖◦·‣\-–*]|\d{1,3}[.)]|[a-zа-яё][.)])\s")
_BULLET_MARKER = re.compile(r"^[•▪➢❖◦·‣\-–*]\s+")
_SENTENCE_END = frozenset('.!?:;»"')
_DIGITS = re.compile(r"\d+")

# Кусок текста блока: служебная строка (разделитель, разметка таблицы) или
# диапазон [start, end) текста строки.
_Piece = str | tuple[PdfLine, int, int]


@dataclass(slots=True)
class _Row:
    cells: dict[int, list[_Piece]] = field(default_factory=dict)


@dataclass(slots=True)
class _Block:
    type: DocumentBlockType
    size: float
    lines: list[PdfLine] = field(default_factory=list)
    pieces: list[_Piece] = field(default_factory=list)
    level: int | None = None
    columns: list[float] = field(default_factory=list)
    rows: list[_Row] = field(default_factory=list)


@dataclass(slots=True)
class PdfLayout:
    """Восстановленная структура документа; из неё собираются все представления."""

    blocks: list[_Block]
    pages_without_text: tuple[int, ...]

    def to_document(self) -> tuple[ParsedDocument, TextRuns]:
        parts: list[str] = []
        runs = TextRuns()
        offset = 0
        spans: list[tuple[_Block, int, int]] = []
        for index, block in enumerate(self.blocks):
            if index:
                parts.append("\n")
                offset += 1
            start = offset
            for piece in _block_pieces(block):
                offset = _emit(piece, parts, runs, offset)
            spans.append((block, start, offset))
        plain_text = "".join(parts)
        builder = DocumentBlocksBuilder(len(plain_text))
        sections: list[DocumentSection] = []
        for block, start, end in spans:
            builder.add(block.type, start, end, heading_level=block.level)
            if block.type is DocumentBlockType.HEADING:
                if sections:
                    sections[-1].end_offset = start
                title = heading_title(plain_text[start:end]) or "Без названия"
                sections.append(DocumentSection(ref=title, start_offset=start, end_offset=start))
        if sections:
            sections[-1].end_offset = len(plain_text)
        return ParsedDocument(plain_text, sections, builder.build()), runs

    def iter_source_fragments(self) -> Iterator[tuple[SourceFragment, dict[str, Any]]]:
        for block in self.blocks:
            if block.type is DocumentBlockType.TABLE:
                yield _table_fragment(block), _rects_locator(block.lines)
                continue
            text = "".join(_piece_text(p) for p in _block_pieces(block))
            if block.type is DocumentBlockType.LIST_ITEM:
                # Единый формат сам ставит «- », второй маркер из файла был бы лишним.
                text = _BULLET_MARKER.sub("", text, count=1)
            yield SourceFragment(block.type, text, block.level), _rects_locator(block.lines)


def build_layout(pages: Sequence[PdfPageLines]) -> PdfLayout:
    repeated = _repeated_line_keys(pages)
    kept_pages = []
    for page in pages:
        edge = _edge_lines(page)
        kept = [line for line in page.lines if not _repeated(line, page, edge, repeated)]
        kept_pages.append((page, kept))
    body_size = _body_size(line for _, lines in kept_pages for line in lines)
    blocks: list[_Block] = []
    for page, lines in kept_pages:
        _append_page(blocks, page, lines, body_size)
    _assign_heading_levels(blocks)
    empty = tuple(page.number for page in pages if not page.lines)
    return PdfLayout(blocks, empty)


# --------------------------------------------------------------------------- страница


def _append_page(
    blocks: list[_Block], page: PdfPageLines, lines: list[PdfLine], body_size: float
) -> None:
    if not lines:
        return
    spacing = _line_spacing(lines, body_size)
    text_left = min(line.left for line in lines)
    text_right = max(line.right for line in lines)
    last = blocks[-1] if blocks else None
    carried = (
        last.columns
        if last is not None
        and last.type is DocumentBlockType.TABLE
        and last.lines[-1].page == page.number - 1
        else None
    )
    tables = _table_ranges(lines, carried)
    prev: PdfLine | None = None
    i = 0
    while i < len(lines):
        table = tables.get(i)
        if table is not None:
            end, columns = table
            _append_table(blocks, lines[i:end], columns, continues_page=i == 0)
            prev = lines[end - 1]
            i = end
            continue
        line = lines[i]
        kind = _line_kind(line, body_size, text_right - text_left)
        last = blocks[-1] if blocks else None
        if (
            kind is DocumentBlockType.HEADING
            and last is not None
            and last.type is DocumentBlockType.PARAGRAPH
            and prev is last.lines[-1]
            and abs(line.size - last.size) <= _SIZE_CHANGE_RATIO * last.size
        ):
            # Короткая последняя строка крупного абзаца остаётся частью абзаца.
            kind = DocumentBlockType.PARAGRAPH
        if last is not None and _continues(last, line, kind, prev, spacing, text_right, page):
            _join_line(last, line)
        else:
            block = _Block(kind, line.size)
            block.lines.append(line)
            block.pieces.append((line, 0, len(line.text)))
            blocks.append(block)
        prev = line
        i += 1


def _line_kind(line: PdfLine, body_size: float, text_width: float) -> DocumentBlockType:
    # Строка во всю ширину текста крупным кеглем обычно абзац, набранный крупно.
    if (
        line.size >= body_size * _HEADING_SIZE_RATIO
        and len(line.text) <= _HEADING_MAX_CHARS
        and line.right - line.left < text_width * _HEADING_MAX_WIDTH_SHARE
    ):
        return DocumentBlockType.HEADING
    if _LIST_MARKER.match(line.text):
        return DocumentBlockType.LIST_ITEM
    return DocumentBlockType.PARAGRAPH


def _continues(
    block: _Block,
    line: PdfLine,
    kind: DocumentBlockType,
    prev: PdfLine | None,
    spacing: float,
    text_right: float,
    page: PdfPageLines,
) -> bool:
    if block.type is DocumentBlockType.TABLE or kind is DocumentBlockType.LIST_ITEM:
        return False
    if (block.type is DocumentBlockType.HEADING) != (kind is DocumentBlockType.HEADING):
        return False
    if abs(line.size - block.size) > _SIZE_CHANGE_RATIO * block.size:
        return False
    last = block.lines[-1]
    if prev is None or prev is not last:
        # Первая строка страницы продолжает абзац прошлой, если тот не закончил фразу.
        return last.page == page.number - 1 and not _ends_sentence(last.text)
    if prev.baseline - line.baseline > spacing * _PARAGRAPH_GAP_RATIO:
        return False
    if _ends_sentence(prev.text) and _next_word_fits(prev, line, text_right):
        return False
    if block.type is DocumentBlockType.LIST_ITEM:
        # Продолжение пункта стоит правее маркера; строка от края маркера уже абзац.
        return line.left > block.lines[0].left + _COLUMN_TOLERANCE
    return True


def _join_line(block: _Block, line: PdfLine) -> None:
    last = block.lines[-1]
    piece = block.pieces[-1]
    if last.hyphenated and isinstance(piece, tuple) and piece[2] - piece[1] > 1:
        block.pieces[-1] = (piece[0], piece[1], piece[2] - 1)
    elif last.text.endswith("-") and line.text[:1].islower():
        pass
    else:
        block.pieces.append(" ")
    block.pieces.append((line, 0, len(line.text)))
    block.lines.append(line)


def _next_word_fits(prev: PdfLine, line: PdfLine, text_right: float) -> bool:
    """Первое слово следующей строки поместилось бы в конец предыдущей.

    Тогда перенос сделал не набор, а конец абзаца: так абзацы без отбивки не слипаются.
    Ширину пробела берём с запасом, чтобы выключка по ширине не дала ложный конец.
    """
    space = _WORD_FIT_SPACE_EM * prev.size
    return prev.right + space + line.first_word_width < text_right


def _ends_sentence(text: str) -> bool:
    return bool(text) and text[-1] in _SENTENCE_END


def _line_spacing(lines: list[PdfLine], body_size: float) -> float:
    """Обычный шаг базовой линии на странице: медиана шагов соседних строк одного кегля."""
    steps = [
        prev.baseline - line.baseline
        for prev, line in zip(lines, lines[1:], strict=False)
        if abs(prev.size - line.size) <= _SIZE_CHANGE_RATIO * prev.size
        and 0 < prev.baseline - line.baseline < 3 * line.size
    ]
    return median(steps) if steps else 1.2 * body_size


def _body_size(lines: Iterator[PdfLine]) -> float:
    """Медианный кегль по числу символов: им набран основной текст."""
    weights: Counter[float] = Counter()
    for line in lines:
        weights[round(line.size, 1)] += len(line.text)
    if not weights:
        return 0.0
    total = sum(weights.values())
    seen = 0
    for size in sorted(weights):
        seen += weights[size]
        if seen * 2 >= total:
            return size
    return 0.0


def _assign_heading_levels(blocks: list[_Block]) -> None:
    sizes = sorted(
        {round(b.size * 2) / 2 for b in blocks if b.type is DocumentBlockType.HEADING},
        reverse=True,
    )
    level_by_size = {size: min(index + 1, 6) for index, size in enumerate(sizes)}
    for block in blocks:
        if block.type is DocumentBlockType.HEADING:
            block.level = level_by_size[round(block.size * 2) / 2]


# --------------------------------------------------------------------------- колонтитулы


def _repeated_line_keys(pages: Sequence[PdfPageLines]) -> frozenset[tuple[int, str]]:
    if len(pages) < 2:
        return frozenset()
    pages_by_key: Counter[tuple[int, str]] = Counter()
    for page in pages:
        keys = {_raw_key(line) for line in _edge_lines(page)}
        pages_by_key.update(keys)
    threshold = len(pages) * _REPEATED_MIN_PAGE_SHARE
    return frozenset(key for key, count in pages_by_key.items() if count > threshold)


def _edge_lines(page: PdfPageLines) -> list[PdfLine]:
    """Кандидаты в колонтитулы: крайние строки страницы у её верхнего или нижнего края.

    Перенос текста ячейки на новую страницу тоже даёт одинаковую строку на одной высоте,
    если строки таблицы одной высоты; такая строка продолжает ячейку соседней строки
    таблицы и колонтитулом не считается.
    """
    lines = page.lines
    count = min(_REPEATED_EDGE_LINES, len(lines))
    edge: list[PdfLine] = []
    for index in [*range(count), *range(max(len(lines) - count, count), len(lines))]:
        line = lines[index]
        if not _near_edge(line, page):
            continue
        inner = index + 1 if index < count else index - 1
        if 0 <= inner < len(lines) and _continues_cells_of(lines[inner], line):
            continue
        edge.append(line)
    return edge


def _continues_cells_of(row: PdfLine, line: PdfLine) -> bool:
    columns = sorted(_lefts(row))
    return len(columns) >= _MIN_TABLE_COLUMNS and _is_continuation(columns, _lefts(line))


def _repeated(
    line: PdfLine, page: PdfPageLines, edge: list[PdfLine], repeated: frozenset[tuple[int, str]]
) -> bool:
    return bool(repeated) and any(line is e for e in edge) and _raw_key(line) in repeated


def _raw_key(line: PdfLine) -> tuple[int, str]:
    """Высота и текст строки. Номер страницы меняется, поэтому цифры не различаются, но
    только в коротких строках вроде «Страница 3 из 10»: иначе одинаковыми оказались бы
    строки таблицы, которые отличаются только числами."""
    text = line.text
    without_digits = _DIGITS.sub("#", text)
    if len(without_digits.replace("#", "").strip()) <= _PAGE_NUMBER_MAX_LETTERS:
        text = without_digits
    return round(line.baseline / _REPEATED_BASELINE_STEP), text


def _near_edge(line: PdfLine, page: PdfPageLines) -> bool:
    edge = page.height * _REPEATED_EDGE_SHARE
    return line.top >= page.height - edge or line.bottom <= edge


# --------------------------------------------------------------------------- таблицы


def _table_ranges(
    lines: list[PdfLine], carried: list[float] | None = None
) -> dict[int, tuple[int, list[float]]]:
    """Начало таблицы → (конец, левые границы колонок). Один проход по строкам.

    Таблица начинается с двух подряд строк, у которых не меньше двух ячеек с общими
    левыми границами. Дальше в неё входят строки с двумя совпавшими границами и
    строки-продолжения: перенос текста внутри ячейки даёт строку, которая начинается
    не с первой колонки. carried — колонки таблицы, которой кончилась прошлая страница:
    её продолжение может начаться и со строки-продолжения.
    """
    ranges: dict[int, tuple[int, list[float]]] = {}
    if carried and lines and _is_row(carried, _lefts(lines[0])):
        columns = list(carried)
        ranges[0] = (_table_end(lines, 1, columns), columns)
    i = ranges[0][0] if ranges else 0
    while i + 1 < len(lines):
        first, second = _lefts(lines[i]), _lefts(lines[i + 1])
        if len(first) < _MIN_TABLE_COLUMNS or _matches(first, second) < _MIN_TABLE_COLUMNS:
            i += 1
            continue
        columns = sorted(first)
        _merge_columns(columns, second)
        end = _table_end(lines, i + 2, columns)
        ranges[i] = (end, columns)
        i = end
    return ranges


def _table_end(lines: list[PdfLine], start: int, columns: list[float]) -> int:
    end = start
    while end < len(lines) and _is_row(columns, _lefts(lines[end])):
        end += 1
    return end


def _is_row(columns: list[float], lefts: list[float]) -> bool:
    """Строка таблицы с такими колонками: новая строка или продолжение ячеек; колонки
    пополняются границами новой строки."""
    if len(lefts) >= _MIN_TABLE_COLUMNS and _matches(columns, lefts) >= _MIN_TABLE_COLUMNS:
        _merge_columns(columns, lefts)
        return True
    return _is_continuation(columns, lefts)


def _lefts(line: PdfLine) -> list[float]:
    return [left for left, _, _ in line.cells]


def _column_of(columns: list[float], left: float) -> int | None:
    index = bisect_left(columns, left - _COLUMN_TOLERANCE)
    if index < len(columns) and abs(columns[index] - left) <= _COLUMN_TOLERANCE:
        return index
    return None


def _matches(columns: list[float], lefts: list[float]) -> int:
    ordered = sorted(columns)
    return sum(1 for left in lefts if _column_of(ordered, left) is not None)


def _merge_columns(columns: list[float], lefts: list[float]) -> None:
    for left in lefts:
        if _column_of(columns, left) is None:
            columns.insert(bisect_left(columns, left), left)


def _is_continuation(columns: list[float], lefts: list[float]) -> bool:
    if not lefts or lefts[0] <= columns[0] + _COLUMN_TOLERANCE:
        return False
    return all(_column_of(columns, left) is not None for left in lefts)


def _append_table(
    blocks: list[_Block], lines: list[PdfLine], columns: list[float], *, continues_page: bool
) -> None:
    last = blocks[-1] if blocks else None
    if (
        continues_page
        and last is not None
        and last.type is DocumentBlockType.TABLE
        and last.lines[-1].page == lines[0].page - 1
        and _matches(last.columns, columns) >= _MIN_TABLE_COLUMNS
    ):
        # Таблица с переносом на следующую страницу остаётся одним блоком.
        block = last
        _merge_columns(block.columns, columns)
    else:
        block = _Block(DocumentBlockType.TABLE, lines[0].size, columns=list(columns))
        blocks.append(block)
    for line in lines:
        block.lines.append(line)
        lefts = _lefts(line)
        if not block.rows or not _is_continuation(block.columns, lefts):
            block.rows.append(_Row())
        row = block.rows[-1]
        for left, start, end in line.cells:
            column = _column_of(block.columns, left)
            if column is None:
                column = max(bisect_left(block.columns, left) - 1, 0)
            cell = row.cells.setdefault(column, [])
            if cell:
                cell.append(" ")
            cell.append((line, start, end))


def _block_pieces(block: _Block) -> Iterator[_Piece]:
    if block.type is not DocumentBlockType.TABLE:
        yield from block.pieces
        return
    width = len(block.columns)
    for row_index, row in enumerate(block.rows):
        if row_index:
            yield "\n"
        yield "|"
        for column in range(width):
            yield " "
            yield from row.cells.get(column, ())
            yield " |"


# --------------------------------------------------------------------------- вывод


def _piece_text(piece: _Piece) -> str:
    if isinstance(piece, str):
        return piece
    line, start, end = piece
    return line.text[start:end]


def _emit(piece: _Piece, parts: list[str], runs: TextRuns, offset: int) -> int:
    if isinstance(piece, str):
        parts.append(piece)
        return offset + len(piece)
    line, start, end = piece
    for run_start, char_index, count in line.runs:
        lo, hi = max(start, run_start), min(end, run_start + count)
        if lo < hi:
            runs.append(offset + lo - start, line.page, char_index + lo - run_start, hi - lo)
    parts.append(line.text[start:end])
    return offset + end - start


def _table_fragment(block: _Block) -> SourceFragment:
    """Таблица с частями по строкам: основание из одной строки подсветит только её."""
    texts: list[str] = []
    parts: list[FragmentPart] = []
    offset = 0
    width = len(block.columns)
    for row in block.rows:
        if texts:
            texts.append("\n")
            offset += 1
        pieces: list[_Piece] = ["|"]
        for column in range(width):
            pieces.append(" ")
            pieces.extend(row.cells.get(column, ()))
            pieces.append(" |")
        row_text = "".join(_piece_text(piece) for piece in pieces)
        # Строка таблицы может состоять из нескольких строк PDF, если ячейка переносится.
        lines = list({id(p[0]): p[0] for p in pieces if not isinstance(p, str)}.values())
        if lines:
            parts.append(FragmentPart(offset, offset + len(row_text), _rects_locator(lines)))
        texts.append(row_text)
        offset += len(row_text)
    return SourceFragment(DocumentBlockType.TABLE, "".join(texts), parts=tuple(parts))


def _rects_locator(lines: list[PdfLine]) -> dict[str, Any]:
    return {
        "kind": PDF_RECTS_LOCATOR,
        "rects": [
            [
                line.page,
                round(line.left, 1),
                round(line.bottom, 1),
                round(line.right, 1),
                round(line.top, 1),
            ]
            for line in lines
        ],
    }
