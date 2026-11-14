"""Основное содержимое HTML-страниц: навигация, скрытые элементы, списки, таблицы, кодировки."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.domain.exceptions import SourceFetchError, UnsupportedFormatError
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.interfaces.source_connector import SourceKind, SourceRef
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.html_structure import extract_html_page
from app.infrastructure.source_connectors.source_content import (
    FetchedContent,
    HtmlOptions,
    SourceContentConverter,
)
from app.infrastructure.source_connectors.text_encoding import decode_text
from app.infrastructure.source_connectors.url_connector import UrlConnector

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "html"
P = DocumentBlockType.PARAGRAPH
H = DocumentBlockType.HEADING
LI = DocumentBlockType.LIST_ITEM
T = DocumentBlockType.TABLE


def _page(name: str, **kwargs: object) -> list[tuple[DocumentBlockType, str]]:
    page = extract_html_page((FIXTURES / name).read_bytes(), **kwargs)  # type: ignore[arg-type]
    return [(f.type, f.text) for f in page.fragments]


def _converter(**options: object) -> SourceContentConverter:
    return SourceContentConverter(DocumentParserRegistry(), HtmlOptions(**options))  # type: ignore[arg-type]


def test_navigation_and_footers_are_dropped_article_header_kept() -> None:
    fragments = _page("chrome.html")

    assert fragments == [
        (H, "Правила загрузки"),
        (P, "Лимит файла 50 МБ, см. раздел 3."),
        (P, "Поддерживаются DOCX и PDF."),
        (P, "Автор статьи: отдел документации"),
    ]


def test_exact_phrase_survives_inline_markup() -> None:
    text = _converter().to_source_text(
        FetchedContent.from_content_type(
            (FIXTURES / "chrome.html").read_bytes(), "text/html", "https://wiki.corp/p"
        )
    )

    assert "50 МБ, см. раздел" in text
    assert "Главная" not in text
    assert "все права защищены" not in text
    assert "Похожие статьи" not in text


def test_hidden_elements_are_dropped() -> None:
    assert _page("hidden.html") == [(P, "Видимый текст."), (P, "Абзац со словом.")]


def test_nested_list_items_are_separate() -> None:
    assert _page("nested_lists.html") == [
        (LI, "Первый пункт"),
        (LI, "Вложенный а"),
        (LI, "Вложенный б"),
        (LI, "Пункт с абзацем"),
        (LI, "Строка с переносом"),
    ]


def test_colspan_is_padded_to_row_width() -> None:
    assert _page("colspan.html") == [
        (
            T,
            "| Тариф | Лимит | Цена |\n| Без ограничений |  | 100 |\n| Базовый | 50 МБ | 0 |",
        )
    ]


def test_host_selector_picks_content_root() -> None:
    fragments = _page("selector.html", content_selector="#main-content")

    assert fragments == [(H, "Порядок согласования"), (P, "Заявку согласует руководитель.")]


def test_selector_matches_host_suffix() -> None:
    options = HtmlOptions(content_selectors={".corp": "#main-content"})

    assert options.selector_for("wiki.corp") == "#main-content"
    assert options.selector_for("corp") is None


def test_overfiltering_falls_back_to_body() -> None:
    html = "<body><main><p>Да</p></main>" + "<div><p>Основной текст страницы.</p></div>" * 5
    page = extract_html_page(html, min_kept_share=0.1)

    assert page.fell_back is True
    assert len(page.fragments) == 6


def test_script_page_asks_to_upload_file() -> None:
    content = FetchedContent.from_content_type(
        (FIXTURES / "script_page.html").read_bytes(), "text/html", "https://app.corp/"
    )

    with pytest.raises(SourceFetchError, match="формируется скриптами"):
        _converter().to_source_text(content)


def test_short_static_page_is_accepted() -> None:
    content = FetchedContent.from_content_type(
        "<p>Коротко.</p>".encode(), "text/html", "https://a.corp/"
    )

    assert _converter().to_source_text(content) == "Коротко."


def test_cp1251_page_without_meta_is_read() -> None:
    raw = (FIXTURES / "cp1251_no_meta.html").read_bytes()
    content = FetchedContent.from_content_type(raw, "text/html", "https://old.corp/")

    text = _converter(min_chars=0).to_source_text(content)

    assert "Срок хранения журналов составляет 90 дней." in text


def test_declared_charset_is_ignored_when_bytes_disagree() -> None:
    raw = "Привет".encode("cp1251")

    assert decode_text(raw, "utf-8", fallback="cp1251") == "Привет"


def test_meta_charset_is_used_for_html() -> None:
    raw = '<meta charset="koi8-r"><p>Привет</p>'.encode("koi8-r")

    assert "Привет" in decode_text(raw, None, fallback="cp1251", sniff_meta=True)


def test_bom_wins_over_declared_charset() -> None:
    raw = "\ufeffТекст".encode("utf-8")

    assert decode_text(raw, "cp1251", fallback="cp1251") == "Текст"


def test_docx_by_url_goes_to_docx_parser() -> None:
    import io

    from docx import Document

    document = Document()
    document.add_heading("Раздел", level=1)
    document.add_paragraph("Текст раздела")
    buffer = io.BytesIO()
    document.save(buffer)
    content = FetchedContent.from_content_type(
        buffer.getvalue(), "application/octet-stream", "https://files.corp/spec.docx"
    )

    assert _converter().to_source_text(content) == "# Раздел\n\nТекст раздела"


def test_pdf_and_unknown_types_are_rejected() -> None:
    for media_type, url in (
        ("application/zip", "https://a.corp/x.zip"),
        ("image/png", "https://a.corp/x.png"),
        ("application/octet-stream", "https://a.corp/blob"),
    ):
        content = FetchedContent.from_content_type(b"\x00\x01", media_type, url)
        with pytest.raises(UnsupportedFormatError, match="не поддерживается"):
            _converter().to_source_text(content)


def test_content_type_parameters_are_parsed() -> None:
    content = FetchedContent.from_content_type(b"", 'Text/HTML; Charset="Windows-1251"', "u")

    assert (content.media_type, content.charset) == ("text/html", "windows-1251")


def test_saved_html_file_uses_same_extraction() -> None:
    registry = DocumentParserRegistry()
    raw = (FIXTURES / "chrome.html").read_bytes()

    text = registry.extract_source_text("projects/p/sources/s.html", raw)
    parsed = registry.parse_by_filename("page.htm", raw)

    assert text.startswith("# Правила загрузки\n\nЛимит файла 50 МБ, см. раздел 3.")
    assert "Главная" not in text
    assert parsed.plain_text.splitlines()[0] == "Правила загрузки"
    assert [b.type for b in parsed.blocks][:2] == [H, P]


async def test_connector_reports_final_url_after_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "/files/spec.md"})
        return httpx.Response(200, content=b"# A\n", headers={"content-type": "text/plain"})

    async def resolve(host: str, port: int) -> list:
        return [ipaddress.ip_address("93.184.216.34")]

    connector = UrlConnector(resolver=resolve, transport=httpx.MockTransport(handler))
    ref = SourceRef(
        id=uuid.uuid4(),
        name="spec",
        type=SourceKind.URL,
        storage_key=None,
        url="https://example.com/go",
        uploaded_at=datetime.now(UTC),
    )

    content = await connector.fetch_content(ref)

    assert content.final_url == "https://example.com/files/spec.md"
    assert await connector.fetch_structured(ref) == "# A\n"
