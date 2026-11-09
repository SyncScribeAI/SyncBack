"""Единый текстовый формат источника для ML-конвейера.

Источник может быть .docx, .md, .txt или страницей по ссылке; конвейер получает его
всегда в одном виде: заголовки «#», пункты списков «- », строки таблиц «| … |»,
код в ограждении, блоки разделены пустой строкой. Так структура источника не зависит
от формата файла, а сравнение «документ ↔ источник» опирается на одинаковую разметку.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.exceptions import LLMInputTooLargeError
from app.domain.interfaces.document_parser import DocumentBlockType

_MAX_HEADING_LEVEL = 6
_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True, slots=True)
class SourceFragment:
    type: DocumentBlockType
    text: str
    heading_level: int | None = None


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
