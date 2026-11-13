"""Какие файлы принимаются источником и что о файле сообщается при загрузке."""

from dataclasses import dataclass
from pathlib import PurePath

from app.domain.exceptions import UnsupportedFormatError

# Неизвестный двоичный файл разобрался бы как текст и дал бы конвейеру мусор вместо
# фактов, поэтому источник принимается только в форматах, которые есть у парсеров.
SOURCE_FILE_EXTENSIONS: tuple[str, ...] = (".docx", ".markdown", ".md", ".pdf", ".txt")


def ensure_source_extension(filename: str) -> None:
    suffix = PurePath(filename).suffix.lower()
    if suffix not in SOURCE_FILE_EXTENSIONS:
        allowed = ", ".join(SOURCE_FILE_EXTENSIONS)
        shown = suffix or "без расширения"
        raise UnsupportedFormatError(
            f"Файл {shown} нельзя загрузить источником. Поддерживаются {allowed}"
        )


@dataclass(frozen=True, slots=True)
class SourceFileReport:
    """Итог проверки файла при загрузке.

    pages_without_text: страницы PDF без текстового слоя (с единицы). Файл принят, но
    текст этих страниц в анализ не попадёт, поэтому пользователь видит их номера сразу.
    """

    pages_without_text: tuple[int, ...] = ()
