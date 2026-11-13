"""Единый текстовый формат источника для ML-конвейера.

Источник может быть .docx, .md, .txt или страницей по ссылке; конвейер получает его
всегда в одном виде: заголовки «#», пункты списков «- », строки таблиц «| … |»,
код в ограждении, блоки разделены пустой строкой. Так структура источника не зависит
от формата файла, а сравнение «документ ↔ источник» опирается на одинаковую разметку.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import LLMInputTooLargeError
from app.domain.interfaces.document_parser import DocumentBlockType

_MAX_HEADING_LEVEL = 6
_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True, slots=True)
class FragmentPart:
    """Часть фрагмента со своим местом в файле, например строка таблицы.

    start и end отсчитываются от начала text фрагмента. Части нужны, когда фрагмент
    велик: таблица PDF может занимать несколько страниц, и основание из одной строки
    должно подсвечивать эту строку, а не всю таблицу.
    """

    start: int
    end: int
    locator: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceFragment:
    type: DocumentBlockType
    text: str
    heading_level: int | None = None
    parts: tuple[FragmentPart, ...] = ()


def serialize_source(fragments: Iterable[SourceFragment]) -> str:
    """O(M): каждый фрагмент форматируется один раз, результат собирается одним join.

    Принимает итератор, чтобы парсер мог отдавать фрагменты по одному, не собирая
    промежуточный список блоков.
    """
    parts: list[str] = []
    for fragment in fragments:
        text = fragment.text.strip()
        if text:
            parts.append(_format(fragment.type, text, fragment.heading_level))
    return _BLOCK_SEPARATOR.join(parts)


@dataclass(frozen=True, slots=True)
class FragmentSpan:
    """Где фрагмент стоит в тексте источника и где он стоит в исходном файле.

    locator описывает место в файле в терминах формата, например диапазон порядковых
    номеров непустых абзацев DOCX. По нему просмотр подсвечивает основание в оригинале.
    """

    start: int
    end: int
    locator: Mapping[str, Any]
    # Части в координатах текста источника, упорядочены по start.
    parts: tuple["FragmentSpan", ...] = ()

    def locator_for(self, start: int, end: int) -> Mapping[str, Any]:
        """Место диапазона [start, end) в файле: части, которые он задевает, или весь фрагмент.

        Локаторы нескольких частей объединяются, если у них одинаковые ключи, а
        различаются только списки; иначе возвращается локатор фрагмента. O(P) по числу
        частей одного фрагмента: оснований у правки единицы, частей не больше строк таблицы.
        """
        if not self.parts:
            return self.locator
        touched: list[Mapping[str, Any]] = []
        for part in self.parts:
            if part.start >= max(end, start + 1):
                break
            if part.end > start:
                touched.append(part.locator)
        if not touched:
            return self.locator
        return _merge_locators(touched) or self.locator


@dataclass(frozen=True, slots=True)
class SourceLayout:
    """Текст источника вместе с положением фрагментов в исходном файле.

    spans упорядочены по start; пустой кортеж значит, что формат положения не знает
    (текст, Markdown, страница по ссылке) и подсветка идёт только по тексту снимка.
    """

    text: str
    spans: tuple[FragmentSpan, ...] = ()


def serialize_source_layout(
    fragments: Iterable[tuple[SourceFragment, Mapping[str, Any]]],
) -> SourceLayout:
    """Тот же текст, что serialize_source, и диапазон каждого фрагмента в нём. O(M)."""
    parts: list[str] = []
    spans: list[FragmentSpan] = []
    offset = 0
    for fragment, locator in fragments:
        text = fragment.text.strip()
        if not text:
            continue
        if parts:
            offset += len(_BLOCK_SEPARATOR)
        formatted = _format(fragment.type, text, fragment.heading_level)
        parts.append(formatted)
        spans.append(
            FragmentSpan(
                offset,
                offset + len(formatted),
                locator,
                _shifted_parts(fragment, text, formatted, offset),
            )
        )
        offset += len(formatted)
    return SourceLayout(_BLOCK_SEPARATOR.join(parts), tuple(spans))


def _shifted_parts(
    fragment: SourceFragment, text: str, formatted: str, offset: int
) -> tuple[FragmentSpan, ...]:
    """Части в координатах текста источника.

    Заголовок при форматировании сворачивает пробелы, и смещения частей в нём теряют
    смысл, поэтому части берутся только у фрагментов, чей текст входит в формат целиком.
    """
    if not fragment.parts:
        return ()
    inner = formatted.find(text)
    if inner < 0:
        return ()
    shift = offset + inner - (len(fragment.text) - len(fragment.text.lstrip()))
    limit = offset + inner + len(text)
    return tuple(
        FragmentSpan(max(shift + part.start, offset), min(shift + part.end, limit), part.locator)
        for part in fragment.parts
        if shift + part.end > offset + inner and shift + part.start < limit
    )


def _merge_locators(locators: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    first = locators[0]
    if len(locators) == 1:
        return first
    if any(locator.keys() != first.keys() for locator in locators):
        return None
    merged: dict[str, Any] = {}
    for key, value in first.items():
        values = [locator[key] for locator in locators]
        if all(isinstance(item, list) for item in values):
            merged[key] = [entry for item in values for entry in item]
        elif all(item == value for item in values):
            merged[key] = value
        else:
            return None
    return merged


def _format(block_type: DocumentBlockType, text: str, heading_level: int | None) -> str:
    if block_type is DocumentBlockType.HEADING:
        level = min(max(heading_level or 1, 1), _MAX_HEADING_LEVEL)
        # Перевод строки внутри заголовка разорвал бы разметку «#» на две строки.
        return f"{'#' * level} {' '.join(text.split())}"
    if block_type is DocumentBlockType.LIST_ITEM:
        return f"- {text}"
    if block_type is DocumentBlockType.CODE:
        return f"```\n{text}\n```"
    return text


def has_paragraph_separator(text: str) -> bool:
    """Есть ли между непустыми строками хотя бы одна пустая (из пробелов тоже). O(M)."""
    seen_content = False
    pending_blank = False
    for line in text.split("\n"):
        if line.strip():
            if pending_blank:
                return True
            seen_content = True
        else:
            pending_blank = seen_content
    return False


def paragraphize(text: str) -> str:
    """Текст без пустых строк-разделителей: каждая непустая строка — отдельный абзац.

    Нужен для источников без структуры (txt, text/plain): иначе соседние строки
    слиплись бы для конвейера в один абзац. Если разделители уже есть, абзацы
    сохраняются, а лишние пустые строки схлопываются. O(M).
    """
    lines = text.split("\n")
    if not has_paragraph_separator(text):
        return _BLOCK_SEPARATOR.join(line.strip() for line in lines if line.strip())

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line.rstrip())
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return _BLOCK_SEPARATOR.join(paragraphs)


def ensure_source_fits(source_name: str, text: str, max_chars: int) -> None:
    """Источник больше лимита не обрезается молча: обрезка потеряла бы факты,
    против которых проверяется документ, и правки стали бы ложными."""
    if len(text) > max_chars:
        raise LLMInputTooLargeError(
            f"Источник «{source_name}» больше лимита: {len(text)} символов при допустимых {max_chars}"
        )
