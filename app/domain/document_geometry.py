"""Положение текста документа на страницах.

Нужен форматам с фиксированной вёрсткой (PDF): правку и цитату там нельзя показать
вставкой в текст, только прямоугольником поверх исходной страницы.
"""

from array import array
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.domain.interfaces.document_parser import ParsedDocument


@dataclass(frozen=True, slots=True)
class PageRect:
    """Прямоугольник в координатах страницы PDF (пользовательское пространство, точки).

    page считается с единицы, как в просмотрщиках. Координаты не учитывают CropBox и
    /Rotate: просмотрщик переводит их в область просмотра сам, поэтому одна запись
    подходит для любого поворота.
    """

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def as_list(self) -> list[float]:
        return [self.page, self.x0, self.y0, self.x1, self.y1]


@dataclass(slots=True)
class TextRuns:
    """Карта plain_text → символы страниц: отрезок [start, start + count) текста идёт
    подряд по символам страницы page, начиная с char_index.

    Хранится колонками array('i'), по 16 байт на отрезок; отрезок обычно равен строке,
    дробится только там, где порядок символов в файле расходится с порядком чтения.
    """

    starts: array[int] = field(default_factory=lambda: array("i"))
    pages: array[int] = field(default_factory=lambda: array("i"))
    char_indexes: array[int] = field(default_factory=lambda: array("i"))
    counts: array[int] = field(default_factory=lambda: array("i"))

    def append(self, start: int, page: int, char_index: int, count: int) -> None:
        self.starts.append(start)
        self.pages.append(page)
        self.char_indexes.append(char_index)
        self.counts.append(count)

    def __len__(self) -> int:
        return len(self.starts)

    def pieces(self, start: int, end: int) -> Iterator[tuple[int, int, int]]:
        """(page, char_index, count) символов, на которые лёг диапазон [start, end).

        Отрезки отсортированы по start, поэтому первый находится бинарным поиском,
        дальше идут только затронутые: O(log R + k).
        """
        i = max(bisect_right(self.starts, start) - 1, 0)
        while i < len(self.starts) and self.starts[i] < end:
            run_start = self.starts[i]
            run_end = run_start + self.counts[i]
            lo, hi = max(start, run_start), min(end, run_end)
            if lo < hi:
                yield self.pages[i], self.char_indexes[i] + lo - run_start, hi - lo
            i += 1


@dataclass(frozen=True, slots=True)
class PageSize:
    """Размер страницы в точках так, как её показывает просмотрщик: с CropBox и поворотом.

    Нужен клиенту, чтобы разметить страницы до отрисовки и не прыгать при прокрутке.
    """

    number: int
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class DocumentLayout:
    """Разбор документа вместе с положением текста на страницах.

    runs и pages есть только у форматов с фиксированной вёрсткой; у остальных это
    обычный ParsedDocument.
    """

    document: ParsedDocument
    runs: TextRuns | None = None
    pages: tuple[PageSize, ...] = ()


def highlight_span(start: int, end: int) -> tuple[int, int]:
    """Диапазон текста, который подсвечивается на странице.

    У вставки диапазон пустой, а на странице нужен ориентир: берётся символ перед точкой
    вставки, справа от него встанет знак вставки.
    """
    if end > start:
        return start, end
    return (start - 1, start) if start > 0 else (0, 1)


def rects_to_json(rects: tuple[PageRect, ...]) -> dict[str, list[list[float]]] | None:
    """Формат поля geometry правок и флагов; координаты округляются до 0.1 пт."""
    if not rects:
        return None
    return {"rects": [[r.page, *(round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1))] for r in rects]}


def rects_from_json(geometry: object) -> tuple[PageRect, ...]:
    """Прямоугольники из поля geometry (обратное rects_to_json).

    Поле пишется воркером, но читается из JSONB, поэтому записи неверной формы
    пропускаются: одна испорченная запись не должна ломать экспорт всего документа.
    """
    if not isinstance(geometry, dict):
        return ()
    items = geometry.get("rects")
    if not isinstance(items, list):
        return ()
    rects: list[PageRect] = []
    for item in items:
        if not isinstance(item, list) or len(item) != 5:
            continue
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in item):
            continue
        page, x0, y0, x1, y1 = item
        if page < 1 or x1 <= x0 or y1 <= y0:
            continue
        rects.append(PageRect(int(page), float(x0), float(y0), float(x1), float(y1)))
    return tuple(rects)
