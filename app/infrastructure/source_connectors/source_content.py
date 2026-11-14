"""Преобразование загруженного содержимого в текст источника.

Транспорт (UrlConnector) отдаёт байты с типом и кодировкой, а разбор выбирается здесь
по типу содержимого и расширению пути. Файлы и ссылки разбираются одними парсерами из
реестра, поэтому DOCX по ссылке и загруженный DOCX дают одинаковый текст. Неизвестный
тип отклоняется: байты архива или картинки, декодированные как текст, дали бы модели
мусор вместо фактов.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from app.domain.exceptions import SourceFetchError, UnsupportedFormatError
from app.domain.source_text import serialize_source
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.html_structure import (
    DEFAULT_MIN_KEPT_SHARE,
    extract_html_page,
)
from app.infrastructure.source_connectors.text_encoding import (
    DEFAULT_FALLBACK_ENCODING,
    decode_text,
)

logger = logging.getLogger("syncscribe.connectors.content")

DEFAULT_MIN_CHARS: Final = 200

_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_MARKDOWN_TYPES: Final = frozenset({"text/markdown", "text/x-markdown"})
_DOCX_TYPE: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Тип, по которому сервер ничего не сообщает: решает расширение пути.
_GENERIC_TYPES: Final = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_TEXT_SUFFIXES: Final = frozenset({".md", ".markdown", ".txt"})
_HTML_SUFFIXES: Final = frozenset({".html", ".htm"})
_JS_PAGE_MESSAGE: Final = (
    "Страница формируется скриптами и без браузера не читается. "
    "Сохраните её как файл и загрузите как источник"
)


@dataclass(frozen=True, slots=True)
class FetchedContent:
    """Ответ сервера без интерпретации: байты, тип, кодировка и итоговый адрес."""

    raw: bytes
    media_type: str
    charset: str | None
    final_url: str

    @classmethod
    def from_content_type(cls, raw: bytes, content_type: str, final_url: str) -> FetchedContent:
        media_type, _, params = content_type.partition(";")
        charset: str | None = None
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "charset" and value.strip():
                charset = value.strip().strip("\"'").lower()
        return cls(
            raw=raw, media_type=media_type.strip().lower(), charset=charset, final_url=final_url
        )

    @property
    def suffix(self) -> str:
        return PurePosixPath(urlsplit(self.final_url).path).suffix.lower()

    @property
    def host(self) -> str:
        return (urlsplit(self.final_url).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class HtmlOptions:
    """Настройки выделения содержимого; значения по умолчанию являются гипотезами."""

    content_selectors: Mapping[str, str] = field(default_factory=dict)
    min_kept_share: float = DEFAULT_MIN_KEPT_SHARE
    min_chars: int = DEFAULT_MIN_CHARS
    fallback_encoding: str = DEFAULT_FALLBACK_ENCODING

    def selector_for(self, host: str) -> str | None:
        """Селектор для хоста: точное имя важнее суффикса с ведущей точкой."""
        if host in self.content_selectors:
            return self.content_selectors[host]
        for entry, selector in self.content_selectors.items():
            if entry.startswith(".") and host.endswith(entry) and len(host) > len(entry):
                return selector
        return None


class SourceContentConverter:
    def __init__(
        self, parser_registry: DocumentParserRegistry, html: HtmlOptions | None = None
    ) -> None:
        self._parsers = parser_registry
        self._html = html or HtmlOptions()

    def to_source_text(self, content: FetchedContent) -> str:
        media_type, suffix = content.media_type, content.suffix
        generic = media_type in _GENERIC_TYPES
        if media_type in _HTML_TYPES or (generic and suffix in _HTML_SUFFIXES):
            return self._html_text(content)
        if media_type in _MARKDOWN_TYPES or (
            media_type in _GENERIC_TYPES | {"text/plain"} and suffix in (".md", ".markdown")
        ):
            return self._decoded(content, "source.md")
        if media_type == "text/plain" or (generic and suffix in _TEXT_SUFFIXES):
            return self._decoded(content, "source.txt")
        if media_type == _DOCX_TYPE or (generic and suffix == ".docx"):
            return self._parsers.extract_source_text("source.docx", content.raw)
        raise UnsupportedFormatError(
            f"Тип содержимого не поддерживается: {media_type or 'не указан'}"
        )

    def _decoded(self, content: FetchedContent, filename: str) -> str:
        text = decode_text(content.raw, content.charset, fallback=self._html.fallback_encoding)
        return self._parsers.extract_source_text(filename, text.encode("utf-8"))

    def _html_text(self, content: FetchedContent) -> str:
        text = decode_text(
            content.raw, content.charset, fallback=self._html.fallback_encoding, sniff_meta=True
        )
        page = extract_html_page(
            text,
            content_selector=self._html.selector_for(content.host),
            min_kept_share=self._html.min_kept_share,
        )
        if page.chars < self._html.min_chars and page.has_scripts:
            logger.info(
                "Страница похожа на приложение на JavaScript",
                extra={"url": content.final_url, "chars": page.chars},
            )
            raise SourceFetchError(_JS_PAGE_MESSAGE)
        return serialize_source(page.fragments)
