"""Построчный проход по тексту с абсолютными смещениями.

Смещения считаются накопительно от позиции перевода строки, а не через поиск
фрагмента в тексте: так проход остаётся O(T) и не путается на повторяющихся строках.
"""

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    start: int
    # Смещение сразу после последнего непробельного символа; для пустой строки равно start.
    content_start: int
    content_end: int

    @property
    def is_blank(self) -> bool:
        return self.content_start == self.content_end


def iter_lines(text: str) -> Iterator[Line]:
    position = 0
    length = len(text)
    while position <= length:
        newline = text.find("\n", position)
        end = length if newline == -1 else newline
        line = text[position:end]
        stripped_left = len(line) - len(line.lstrip())
        stripped_right = len(line.rstrip())
        content_start = position + min(stripped_left, stripped_right)
        yield Line(
            text=line,
            start=position,
            content_start=content_start,
            content_end=position + stripped_right,
        )
        if newline == -1:
            return
        position = newline + 1
