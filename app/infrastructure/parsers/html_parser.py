"""HtmlParser: сохранённая из браузера страница как файловый источник.

Это рабочий обход для страниц за SSO и страниц на JavaScript: браузер пользователя
уже выполнил вход и скрипты, а файл разбирается тем же выделением основного
содержимого, что и страница по ссылке. Документом для редактирования HTML не бывает:
реестр форматов документов его не принимает, поэтому parse нужен только для
просмотра текста.
"""

from __future__ import annotations

from app.domain.document_blocks import DocumentBlocksBuilder
from app.domain.interfaces.document_parser import ParsedDocument
from app.domain.source_text import SourceFragment, serialize_source
from app.infrastructure.source_connectors.html_structure import extract_html_page
from app.infrastructure.source_connectors.text_encoding import (
    DEFAULT_FALLBACK_ENCODING,
    decode_text,
)


class HtmlParser:
    def __init__(self, fallback_encoding: str = DEFAULT_FALLBACK_ENCODING) -> None:
        self._fallback = fallback_encoding

    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        fragments = self._fragments(raw_bytes)
        text = "\n".join(fragment.text for fragment in fragments)
        builder = DocumentBlocksBuilder(len(text))
        offset = 0
        for fragment in fragments:
            end = offset + len(fragment.text)
            builder.add(fragment.type, offset, end, heading_level=fragment.heading_level)
            offset = end + 1
        return ParsedDocument(plain_text=text, sections=[], blocks=builder.build())

    def extract_text(self, raw_bytes: bytes) -> str:
        return "\n".join(fragment.text for fragment in self._fragments(raw_bytes))

    def extract_source_text(self, raw_bytes: bytes) -> str:
        return serialize_source(self._fragments(raw_bytes))

    def _fragments(self, raw_bytes: bytes) -> list[SourceFragment]:
        text = decode_text(raw_bytes, None, fallback=self._fallback, sniff_meta=True)
        return extract_html_page(text).fragments
