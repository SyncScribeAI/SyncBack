"""Применение правок с известными границами за один проход.

Последовательные str.replace пересобирают строку на каждую правку — O(S·T). Здесь правки
сортируются по началу (O(S log S)), а результат собирается срезами исходной строки
и одним "".join — O(T + S). Сортировка устойчивая: правки с одним началом остаются
в порядке выдачи (position), поэтому несколько абзацев после одного якоря идут так же,
как в списке правок.

Правка, начало которой попадает внутрь уже применённой, пропускается: два принятых
предложения на одно место не могут примениться оба, а угадывать порядок за пользователя
нельзя.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextEdit:
    """Замена text[start:end] на replacement или, при replacement=None, вставка абзаца block.

    Вставка ставит block отдельным абзацем в позицию start (конец строки якоря) и
    оставляет вокруг ровно одну пустую строку — как place_addition для правок без границ.
    """

    start: int
    end: int
    replacement: str | None = None
    block: str = ""


def apply_edits(text: str, edits: Iterable[TextEdit]) -> str:
    out: list[str] = []
    cursor = 0
    # После вставленного абзаца следующий непустой фрагмент отделяется пустой строкой.
    separate = False

    def emit(piece: str) -> None:
        nonlocal separate
        if not piece:
            return
        if separate:
            out.append("\n\n")
            separate = False
        out.append(piece)

    # Курсор после вставки перескакивает пустые строки, а следующая вставка в ту же точку
    # (второй абзац после того же якоря) пересечением не считается.
    last_insert = -1
    for edit in sorted(edits, key=lambda e: e.start):
        same_point = edit.replacement is None and edit.start == last_insert
        if edit.start < cursor and not same_point:
            continue
        emit(text[cursor : edit.start])
        if edit.replacement is not None:
            emit(edit.replacement)
            cursor = edit.end
            continue
        _trim_trailing_newlines(out)
        if out:
            out.append("\n\n")
        out.append(edit.block)
        separate = True
        last_insert = cursor = edit.start
        while cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    emit(text[cursor:])
    if separate:
        out.append("\n")
    return "".join(out)


def _trim_trailing_newlines(out: list[str]) -> None:
    while out:
        trimmed = out[-1].rstrip("\n")
        if trimmed:
            out[-1] = trimmed
            return
        out.pop()
