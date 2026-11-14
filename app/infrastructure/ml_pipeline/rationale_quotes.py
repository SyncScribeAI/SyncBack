"""Цитаты источников из строки обоснования harness.

Структурного поля с цитатами у правок в контракте 2.0 нет, но harness дописывает их в
конец обоснования в двух видах:

    … Источник: «цитата 1» (имя 1) «цитата 2» (имя 2)
    … Источник (имя): «фрагмент источника»

За первым видом может идти «Проверьте внимательно: …». Разбор живёт в адаптере как
антикоррупционный код: формат строки принадлежит harness, домен видит только Quote.
Цитата может содержать кавычки и скобки, а имя источника может содержать скобки, поэтому
строка разбирается с конца и опирается на известные имена источников запроса.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.domain.evidence import Quote

_LIST_MARKER = "Источник: «"
_SINGLE_MARKER_RE = re.compile(r"Источник \(")
_REVIEW_TAIL = " Проверьте внимательно: "
_TRAILING_NAME_RE = re.compile(r"\s\(([^()]*)\)\s*$")


def parse_rationale_quotes(rationale: str | None, source_names: Sequence[str]) -> tuple[Quote, ...]:
    """Цитаты из обоснования; пустой кортеж, если строка не похожа ни на один вид. O(L·N).

    L — длина обоснования, N — число источников запроса.
    """
    if not rationale:
        return ()
    names = sorted({name for name in source_names if name}, key=len, reverse=True)
    list_at = rationale.rfind(_LIST_MARKER)
    single_at = _last_single_marker(rationale)
    if single_at > list_at:
        quote = _parse_single(rationale[single_at:], names)
        if quote is not None:
            return (quote,)
    if list_at >= 0:
        return _parse_list(
            _without_review_tail(rationale[list_at + len(_LIST_MARKER) - 1 :]), names
        )
    return ()


def _last_single_marker(rationale: str) -> int:
    last = -1
    for match in _SINGLE_MARKER_RE.finditer(rationale):
        last = match.start()
    return last


def _without_review_tail(tail: str) -> str:
    cut = tail.rfind(_REVIEW_TAIL)
    return (tail[:cut] if cut >= 0 else tail).rstrip()


def _parse_single(tail: str, names: Sequence[str]) -> Quote | None:
    """«Источник (имя): «фрагмент»». Фрагмент идёт до последней «»» строки."""
    body = tail[len("Источник (") :]
    for name in names:
        prefix = f"{name}): «"
        if body.startswith(prefix):
            return _closed_quote(name, body[len(prefix) :])
    separator = body.find("): «")
    if separator < 0:
        return None
    return _closed_quote(body[:separator], body[separator + len("): «") :])


def _closed_quote(name: str, rest: str) -> Quote | None:
    end = rest.rfind("»")
    if end < 0:
        return None
    text = rest[:end]
    return Quote(source_name=name, text=text) if text.strip() else None


def _parse_list(body: str, names: Sequence[str]) -> tuple[Quote, ...]:
    """«цитата» (имя) «цитата» (имя) с конца: имя, затем начало его цитаты.

    Начало цитаты — ближайшая слева граница «) «», за которой стоит известное имя, или
    начало строки. Так кавычки и скобки внутри цитаты не рвут её на части.
    """
    quotes: list[Quote] = []
    rest = body.rstrip()
    while rest:
        split = _split_trailing_name(rest, names)
        if split is None:
            return ()
        before, name = split
        if not before.endswith("»"):
            return ()
        start = _quote_start(before, names)
        if start < 0:
            return ()
        text = before[start + 1 : -1]
        if text.strip():
            quotes.append(Quote(source_name=name, text=text))
        rest = before[:start].rstrip()
    quotes.reverse()
    return tuple(quotes)


def _split_trailing_name(rest: str, names: Sequence[str]) -> tuple[str, str] | None:
    for name in names:
        suffix = f" ({name})"
        if rest.endswith(suffix):
            return rest[: -len(suffix)].rstrip(), name
    match = _TRAILING_NAME_RE.search(rest)
    if match is None:
        return None
    return rest[: match.start()].rstrip(), match.group(1)


def _quote_start(before: str, names: Sequence[str]) -> int:
    """Позиция «, с которой начинается последняя цитата в before, или -1."""
    position = len(before) - 1
    while True:
        position = before.rfind("«", 0, position)
        if position < 0:
            return -1
        head = before[:position].rstrip()
        if not head:
            return position
        if head.endswith(")") and _ends_with_name(head, names):
            return position


def _ends_with_name(head: str, names: Sequence[str]) -> bool:
    if any(head.endswith(f" ({name})") for name in names):
        return True
    # Имя источника неизвестно (переименован или пришёл из чанка harness): граница
    # принимается по форме «» (…)».
    match = _TRAILING_NAME_RE.search(head)
    return match is not None and head[: match.start()].rstrip().endswith("»")
