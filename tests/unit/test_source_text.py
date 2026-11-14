"""Источник любого формата приходит в ML-конвейер в одном текстовом формате.

Заголовки «#», пункты «- », строки таблиц «| … |», код в ограждении, блоки через
пустую строку — независимо от того, был источник .docx, .md, .txt или страницей.
"""

import io
import uuid
from datetime import UTC, datetime

import httpx
import ipaddress
import pytest
from docx import Document as DocxDocument

from app.domain.exceptions import LLMInputTooLargeError
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.interfaces.source_connector import SourceKind, SourceRef
from app.domain.source_text import (
    SourceFragment,
    ensure_source_fits,
    has_paragraph_separator,
    paragraphize,
    serialize_source,
)
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.html_structure import iter_html_fragments
from app.infrastructure.source_connectors.url_connector import UrlConnector

H = DocumentBlockType.HEADING
P = DocumentBlockType.PARAGRAPH
LI = DocumentBlockType.LIST_ITEM
C = DocumentBlockType.CODE
T = DocumentBlockType.TABLE


# --- сериализация -------------------------------------------------------------


def test_serialize_formats_each_block_type() -> None:
    text = serialize_source(
        [
            SourceFragment(H, "Тарифы", 2),
            SourceFragment(P, "Описание"),
            SourceFragment(LI, "Базовый"),
            SourceFragment(C, "x = 1"),
            SourceFragment(T, "| Цена | 100 |"),
            SourceFragment(P, "   "),
        ]
    )
    assert text == "## Тарифы\n\nОписание\n\n- Базовый\n\n```\nx = 1\n```\n\n| Цена | 100 |"


@pytest.mark.parametrize(("level", "marks"), [(None, "#"), (0, "#"), (3, "###"), (9, "######")])
def test_heading_level_is_clamped(level: int | None, marks: str) -> None:
    assert serialize_source([SourceFragment(H, "A", level)]) == f"{marks} A"


def test_multiline_heading_stays_on_one_line() -> None:
    assert serialize_source([SourceFragment(H, "Раздел\nпродолжение", 1)]) == "# Раздел продолжение"


def test_serialize_accepts_generator() -> None:
    fragments = (SourceFragment(P, str(i)) for i in range(3))
    assert serialize_source(fragments) == "0\n\n1\n\n2"


def test_paragraphize_splits_lines_without_separators() -> None:
    assert paragraphize("  Первая\nВторая  \n\n") == "Первая\n\nВторая"


def test_paragraphize_keeps_existing_paragraphs() -> None:
    assert paragraphize("a\nb\n\n \n\n\nc\n") == "a\nb\n\nc"
    assert has_paragraph_separator("a\n  \nb")
    assert not has_paragraph_separator("\n\na\nb\n\n")


def test_oversized_source_is_rejected_with_its_name() -> None:
    ensure_source_fits("spec", "x" * 10, 10)
    with pytest.raises(LLMInputTooLargeError, match="«spec»"):
        ensure_source_fits("spec", "x" * 11, 10)


# --- файлы ------------------------------------------------------------------


def _docx_source() -> bytes:
    document = DocxDocument()
    document.add_heading("Тарифы", level=1)
    document.add_paragraph("Цены указаны с НДС.")
    document.add_paragraph("Базовый", style="List Bullet")
    table = document.add_table(rows=3, cols=2)
    header = table.cell(0, 0).merge(table.cell(0, 1))
    header.text = "Сводка"
    table.cell(1, 0).text = "Цена"
    cell = table.cell(1, 1)
    cell.text = "100 руб."
    cell.add_table(rows=1, cols=1).cell(0, 0).text = "в месяц"
    document.add_paragraph("")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_source_keeps_structure_and_table_rows() -> None:
    text = DocumentParserRegistry().extract_source_text("s.docx", _docx_source())
    assert text == (
        "# Тарифы\n\nЦены указаны с НДС.\n\n- Базовый\n\n| Сводка |\n| Цена | 100 руб. в месяц |"
    )


def test_markdown_source_is_passed_through_normalized() -> None:
    raw = "# A\r\n\r\n* item\u00a0x\n".encode()
    assert DocumentParserRegistry().extract_source_text("s.md", raw) == "# A\n\n* item x\n"


def test_txt_source_is_paragraphized() -> None:
    assert DocumentParserRegistry().extract_source_text("s.txt", b"one\ntwo\n") == "one\n\ntwo"


# --- HTML -------------------------------------------------------------------


def _fragments(html: str) -> list[tuple[DocumentBlockType, str]]:
    return [(f.type, f.text) for f in iter_html_fragments(html.encode())]


def test_html_blocks_in_page_order() -> None:
    html = """
    <html><head><title>t</title><style>p{}</style></head><body>
      <h1>Сервис <small>v2</small></h1>
      <div>Вводный <b>текст</b><!-- комментарий --></div>
      <div>Строка 1<br>Строка 2</div>
      <ul><li>Пункт <a href="#">один</a></li><li>Два<ul><li>вложенный</li></ul></li></ul>
      <pre>def f():
    return 1</pre>
      <script>alert(1)</script>
      <table><thead><tr><th>Тариф</th><th>Цена</th></tr></thead>
        <tbody><tr><td>Базовый</td><td>100 <table><tr><td>в месяц</td></tr></table></td></tr>
        <tr><td></td><td></td></tr></tbody></table>
      Хвост
    </body></html>
    """
    assert _fragments(html) == [
        (H, "Сервис v2"),
        (P, "Вводный текст"),
        (P, "Строка 1"),
        (P, "Строка 2"),
        (LI, "Пункт один"),
        (LI, "Два"),
        (LI, "вложенный"),
        (C, "def f():\n    return 1"),
        (T, "| Тариф | Цена |\n| Базовый | 100 в месяц |"),
        (P, "Хвост"),
    ]


def test_deeply_nested_html_does_not_hit_recursion_limit() -> None:
    depth = 3000
    html = "<div>" * depth + "<p>глубоко</p>" + "</div>" * depth
    assert _fragments(html) == [(P, "глубоко")]


# --- UrlConnector.fetch_structured ---------------------------------------------


def _ref(url: str) -> SourceRef:
    return SourceRef(
        id=uuid.uuid4(),
        name="spec",
        type=SourceKind.URL,
        storage_key=None,
        url=url,
        uploaded_at=datetime.now(UTC),
    )


def _connector(body: bytes, content_type: str) -> UrlConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    async def resolve(host: str, port: int) -> list:
        return [ipaddress.ip_address("93.184.216.34")]

    return UrlConnector(resolver=resolve, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("url", "content_type", "body", "expected"),
    [
        (
            "https://example.com/page",
            "text/html; charset=utf-8",
            "<h2>Тарифы</h2><p>Цена 100</p>".encode(),
            "## Тарифы\n\nЦена 100",
        ),
        ("https://example.com/README.md", "text/plain", b"# A\n- b\n", "# A\n- b\n"),
        ("https://example.com/x", "text/markdown", b"# A\n", "# A\n"),
        ("https://example.com/notes", "text/plain", "a\nb\u00a0c".encode(), "a\n\nb c"),
    ],
)
async def test_url_fetch_structured_by_content_type(
    url: str, content_type: str, body: bytes, expected: str
) -> None:
    assert await _connector(body, content_type).fetch_structured(_ref(url)) == expected


async def test_url_fetch_keeps_legacy_text() -> None:
    connector = _connector("<h2>Тарифы</h2><p>Цена 100</p>".encode(), "text/html")
    assert await connector.fetch(_ref("https://example.com/page")) == "Тарифы\nЦена 100"
