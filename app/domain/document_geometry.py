"""Положение текста документа на страницах.

Нужен форматам с фиксированной вёрсткой (PDF): правку и цитату там нельзя показать
вставкой в текст, только прямоугольником поверх исходной страницы.
"""

from array import array
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field


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
