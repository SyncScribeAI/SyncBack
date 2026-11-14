"""Основное содержимое HTML-страницы источника: заголовки, абзацы, списки, код, таблицы.

Навигация, подвалы, формы и скрытые элементы в текст источника не попадают: модель
сравнивает документ с фактами страницы, а меню портала дало бы ложные расхождения.
Корень содержимого выбирается по селектору хоста из настроек, затем по единственному
<main> или role="main", затем по единственному <article>, иначе берётся <body>.

Обход итеративный (явный стек): глубоко вложенная разметка не упирается в лимит
рекурсии Python. Исключение проверяется при входе в элемент, и исключённое поддерево
не обходится, поэтому каждый узел читается один раз и сложность остаётся O(n). Если
фильтр оставил слишком малую долю текста, разбор повторяется по <body> только с жёстким
списком пропуска: это второй проход O(n), а не повтор на каждый элемент.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntFlag
from typing import Any, Final

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.source_text import SourceFragment
from app.domain.text_normalization import normalize_text

logger = logging.getLogger("syncscribe.connectors.html")

DEFAULT_MIN_KEPT_SHARE: Final = 0.1
# Объединение шире этого числа колонок считается ошибкой разметки, а не таблицей.
_MAX_COLSPAN: Final = 100

_SKIP = frozenset({"script", "style", "noscript", "head", "template", "svg", "iframe"})
_EXCLUDED_TAGS = frozenset({"nav", "aside", "form", "button", "dialog"})
_PAGE_CHROME = frozenset({"header", "footer"})
_CONTENT_SCOPES = frozenset({"article", "section"})
_EXCLUDED_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search"})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_TEXT_BLOCKS: dict[str, DocumentBlockType] = {
    "p": DocumentBlockType.PARAGRAPH,
    "li": DocumentBlockType.LIST_ITEM,
    "dt": DocumentBlockType.PARAGRAPH,
    "dd": DocumentBlockType.PARAGRAPH,
    "blockquote": DocumentBlockType.PARAGRAPH,
    "figcaption": DocumentBlockType.PARAGRAPH,
}
# Контейнеры сами блоком не являются, но разрывают текст: «<div>a</div><div>b</div>»
# дают два абзаца, а не «ab».
_CONTAINERS = frozenset(
    {
        "html", "body", "main", "article", "section", "div", "header", "footer", "nav",
        "aside", "ul", "ol", "dl", "figure", "form", "fieldset", "details", "summary",
        "address", "center",
    }
)  # fmt: skip
# Внутри ячейки или заголовка эти элементы разделяют слова пробелом.
_INLINE_BREAKS = _CONTAINERS | frozenset(_TEXT_BLOCKS) | {"br", "tr", "td", "th", "table"}
_TABLE_PARTS = frozenset({"thead", "tbody", "tfoot"})


class _Exit(IntFlag):
    """Что сделать при выходе из элемента."""

    NONE = 0
    FLUSH = 1
    END_BLOCK = 2
    END_SCOPE = 4


@dataclass(frozen=True, slots=True)
class HtmlPage:
    """Фрагменты основного содержимого и признаки страницы для решения о загрузке."""

    fragments: list[SourceFragment]
    has_scripts: bool
    fell_back: bool

    @property
    def chars(self) -> int:
        return sum(len(fragment.text) for fragment in self.fragments)


def _attr_text(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or _attr_text(tag, "aria-hidden").strip().lower() == "true":
        return True
    style = _attr_text(tag, "style")
    if not style:
        return False
    compact = "".join(style.lower().split())
    return "display:none" in compact or "visibility:hidden" in compact


def _is_excluded(tag: Tag, scope_depth: int) -> bool:
    """Элемент оформления страницы, а не содержимого."""
    name = tag.name
    if name in _EXCLUDED_TAGS:
        return True
    # Шапка и подвал статьи относятся к её содержимому, шапка сайта нет.
    if name in _PAGE_CHROME and scope_depth == 0:
        return True
    if _attr_text(tag, "role").strip().lower() in _EXCLUDED_ROLES:
        return True
    return _is_hidden(tag)


def _collapse(text: str) -> str:
    return " ".join(normalize_text(text).split())


def _inline_text(node: Tag, filtered: bool) -> str:
    """Текст элемента без разделителя между узлами, со схлопыванием пробелов.

    «50 <b>МБ</b>, см.» даёт «50 МБ, см.»: разделитель между узлами поставил бы
    пробел перед запятой, и точный поиск фразы по источнику перестал бы находить её.
    """
    parts: list[str] = []
    stack: list[Iterator[Any]] = [iter(node.children)]
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
            continue
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if not isinstance(child, Tag) or child.name in _SKIP:
            continue
        if filtered and _is_excluded(child, scope_depth=1):
            continue
        if child.name in _INLINE_BREAKS:
            parts.append(" ")
        stack.append(iter(child.children))
    return _collapse("".join(parts))


def _colspan(cell: Tag) -> int:
    raw = _attr_text(cell, "colspan").strip()
    return min(max(int(raw), 1), _MAX_COLSPAN) if raw.isdigit() else 1


def _row_cells(row: Tag, filtered: bool) -> list[str]:
    cells: list[str] = []
    for cell in row.find_all(["td", "th"], recursive=False):
        if filtered and _is_hidden(cell):
            continue
        cells.append(_inline_text(cell, filtered))
        # Объединённая ячейка дополняется пустыми, чтобы колонки строк совпадали.
        cells.extend("" for _ in range(_colspan(cell) - 1))
    return cells


def _table_rows(table: Tag, filtered: bool) -> Iterator[list[str]]:
    """Строки только этой таблицы: вложенные таблицы входят в текст своей ячейки."""
    for child in table.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "tr":
            yield _row_cells(child, filtered)
        elif child.name in _TABLE_PARTS:
            for row in child.find_all("tr", recursive=False):
                yield _row_cells(row, filtered)


def _leaf_fragment(node: Tag, filtered: bool) -> SourceFragment | None:
    name = node.name
    if name in _HEADINGS:
        text = _inline_text(node, filtered)
        return SourceFragment(DocumentBlockType.HEADING, text, _HEADINGS[name]) if text else None
    if name == "table":
        rows = [row for row in _table_rows(node, filtered) if any(row)]
        if not rows:
            return None
        return SourceFragment(
            DocumentBlockType.TABLE, "\n".join(f"| {' | '.join(row)} |" for row in rows)
        )
    # В коде значимы переводы строк и отступы, поэтому пробелы не схлопываются.
    text = normalize_text(node.get_text()).strip("\n")
    return SourceFragment(DocumentBlockType.CODE, text) if text.strip() else None


def _walk(root: Tag, *, filtered: bool) -> list[SourceFragment]:
    fragments: list[SourceFragment] = []
    inline: list[str] = []
    block_types: list[DocumentBlockType] = []
    scope_depth = 0

    def flush() -> None:
        text = _collapse("".join(inline))
        inline.clear()
        if text:
            kind = block_types[-1] if block_types else DocumentBlockType.PARAGRAPH
            fragments.append(SourceFragment(kind, text))

    stack: list[tuple[Iterator[Any], _Exit]] = [(iter(root.children), _Exit.NONE)]
    while stack:
        children, on_exit = stack[-1]
        node = next(children, None)
        if node is None:
            stack.pop()
            if on_exit & (_Exit.FLUSH | _Exit.END_BLOCK):
                flush()
            if on_exit & _Exit.END_BLOCK:
                block_types.pop()
            if on_exit & _Exit.END_SCOPE:
                scope_depth -= 1
            continue
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            inline.append(str(node))
            continue
        if not isinstance(node, Tag):
            continue
        name = node.name
        if name in _SKIP or (filtered and _is_excluded(node, scope_depth)):
            continue
        if name == "br":
            # Внутри блока <br> разделяет слова; между блоками он разделяет абзацы,
            # как одиночные строки в txt.
            if block_types:
                inline.append(" ")
            else:
                flush()
            continue
        if name == "hr":
            flush()
            continue
        if name in _HEADINGS or name in ("table", "pre"):
            flush()
            if (fragment := _leaf_fragment(node, filtered)) is not None:
                fragments.append(fragment)
            continue

        on_exit = _Exit.NONE
        if name in _TEXT_BLOCKS:
            # Пункт с вложенным списком выдаёт свой текст отдельным пунктом: вложенный
            # контейнер сбрасывает накопленный текст с типом текущего блока.
            flush()
            kind = _TEXT_BLOCKS[name]
            # Абзацы внутри пункта списка остаются пунктом, как в исходной вёрстке.
            if block_types and block_types[-1] is DocumentBlockType.LIST_ITEM and name == "p":
                kind = DocumentBlockType.LIST_ITEM
            block_types.append(kind)
            on_exit |= _Exit.END_BLOCK
        elif name in _CONTAINERS:
            flush()
            on_exit |= _Exit.FLUSH
        if name in _CONTENT_SCOPES:
            scope_depth += 1
            on_exit |= _Exit.END_SCOPE
        stack.append((iter(node.children), on_exit))

    flush()
    return fragments


def _single(found: list[Tag]) -> Tag | None:
    return found[0] if len(found) == 1 else None


def _content_root(soup: BeautifulSoup, selector: str | None) -> Tag:
    if selector:
        try:
            chosen = soup.select_one(selector)
        except ValueError:
            logger.warning("Некорректный селектор содержимого", extra={"selector": selector})
            chosen = None
        if chosen is not None:
            return chosen
    mains = soup.find_all(lambda tag: tag.name == "main" or tag.get("role") == "main")
    if (main := _single(mains)) is not None:
        return main
    if (article := _single(soup.find_all("article"))) is not None:
        return article
    body = soup.find("body")
    return body if isinstance(body, Tag) else soup


def extract_html_page(
    html: bytes | str,
    *,
    content_selector: str | None = None,
    min_kept_share: float = DEFAULT_MIN_KEPT_SHARE,
) -> HtmlPage:
    """Фрагменты основного содержимого страницы.

    Защита от чрезмерной фильтрации: если после фильтра осталось меньше
    min_kept_share текста страницы, возвращается разбор всего <body> только с
    жёстким списком пропуска, а причина пишется в лог.
    """
    soup = BeautifulSoup(html, "html.parser")
    has_scripts = soup.find("script") is not None
    body = soup.find("body")
    page_root = body if isinstance(body, Tag) else soup

    kept = _walk(_content_root(soup, content_selector), filtered=True)
    full = _walk(page_root, filtered=False)
    kept_chars = sum(len(f.text) for f in kept)
    full_chars = sum(len(f.text) for f in full)
    if full_chars and kept_chars < min_kept_share * full_chars:
        logger.info(
            "Фильтр содержимого оставил слишком мало текста, берём всю страницу",
            extra={"kept_chars": kept_chars, "page_chars": full_chars},
        )
        return HtmlPage(fragments=full, has_scripts=has_scripts, fell_back=True)
    return HtmlPage(fragments=kept, has_scripts=has_scripts, fell_back=False)


def iter_html_fragments(
    html: bytes | str,
    *,
    content_selector: str | None = None,
    min_kept_share: float = DEFAULT_MIN_KEPT_SHARE,
) -> Iterator[SourceFragment]:
    yield from extract_html_page(
        html, content_selector=content_selector, min_kept_share=min_kept_share
    ).fragments
