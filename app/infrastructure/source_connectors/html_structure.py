"""Структура HTML-страницы источника: заголовки, абзацы, списки, код, строки таблиц.

Обход итеративный (явный стек): глубоко вложенная разметка не упирается в лимит
рекурсии Python. Внутрь найденного блока обход не спускается, поэтому каждый узел
читается один раз — O(N) по числу узлов, и текст вложенных блоков не дублируется.
"""

from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.source_text import SourceFragment
from app.domain.text_normalization import normalize_text

_SKIP = frozenset({"script", "style", "noscript", "head", "template", "svg", "iframe"})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_LEAF_BLOCKS: dict[str, DocumentBlockType] = {
    "p": DocumentBlockType.PARAGRAPH,
    "li": DocumentBlockType.LIST_ITEM,
    "dt": DocumentBlockType.PARAGRAPH,
    "dd": DocumentBlockType.PARAGRAPH,
    "pre": DocumentBlockType.CODE,
    "blockquote": DocumentBlockType.PARAGRAPH,
    "figcaption": DocumentBlockType.PARAGRAPH,
}
# Контейнеры сами блоком не являются, но разрывают текст: «<div>a</div><div>b</div>»
# — два абзаца, а не «ab».
_CONTAINERS = frozenset(
    {
        "html", "body", "main", "article", "section", "div", "header", "footer", "nav",
        "aside", "ul", "ol", "dl", "figure", "form", "fieldset", "details", "summary",
        "address", "center",
    }
)  # fmt: skip
_TABLE_PARTS = frozenset({"thead", "tbody", "tfoot"})


def iter_html_fragments(raw: bytes) -> Iterator[SourceFragment]:
    soup = BeautifulSoup(raw, "html.parser")
    inline: list[str] = []

    def flush() -> SourceFragment | None:
        text = _collapse("".join(inline))
        inline.clear()
        return SourceFragment(DocumentBlockType.PARAGRAPH, text) if text else None

    # Элемент стека — итератор по детям; None — маркер «после контейнера сбросить текст».
    stack: list[Iterator[Any] | None] = [iter(soup.children)]
    while stack:
        children = stack[-1]
        if children is None:
            stack.pop()
            if (fragment := flush()) is not None:
                yield fragment
            continue
        node = next(children, None)
        if node is None:
            stack.pop()
            continue
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            inline.append(str(node))
            continue
        if not isinstance(node, Tag):
            continue
        name = node.name
        if name in _SKIP:
            continue
        if name in ("br", "hr"):
            # Строки, разделённые <br>, — отдельные абзацы, как одиночные строки в txt.
            if (fragment := flush()) is not None:
                yield fragment
            continue
        if name in _HEADINGS or name in _LEAF_BLOCKS or name == "table":
            if (fragment := flush()) is not None:
                yield fragment
            block = _block_fragment(node)
            if block is not None:
                yield block
            continue
        if name in _CONTAINERS:
            if (fragment := flush()) is not None:
                yield fragment
            stack.append(None)
        stack.append(iter(node.children))

    if (fragment := flush()) is not None:
        yield fragment


def _block_fragment(node: Tag) -> SourceFragment | None:
    name = node.name
    if name in _HEADINGS:
        text = _collapse(node.get_text(" "))
        return SourceFragment(DocumentBlockType.HEADING, text, _HEADINGS[name]) if text else None
    if name == "table":
        rows = [row for row in _table_rows(node) if any(row)]
        if not rows:
            return None
        return SourceFragment(
            DocumentBlockType.TABLE, "\n".join(f"| {' | '.join(row)} |" for row in rows)
        )
    block_type = _LEAF_BLOCKS[name]
    if block_type is DocumentBlockType.CODE:
        # В коде значимы переводы строк и отступы — не схлопываем.
        text = normalize_text(node.get_text()).strip("\n")
    else:
        text = _collapse(node.get_text(" "))
    return SourceFragment(block_type, text) if text.strip() else None


def _table_rows(table: Tag) -> Iterator[list[str]]:
    """Строки только этой таблицы: вложенные таблицы входят в текст своей ячейки."""
    for child in table.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "tr":
            yield _row_cells(child)
        elif child.name in _TABLE_PARTS:
            for row in child.find_all("tr", recursive=False):
                yield _row_cells(row)


def _row_cells(row: Tag) -> list[str]:
    return [_collapse(cell.get_text(" ")) for cell in row.find_all(["td", "th"], recursive=False)]


def _collapse(text: str) -> str:
    return " ".join(normalize_text(text).split())
