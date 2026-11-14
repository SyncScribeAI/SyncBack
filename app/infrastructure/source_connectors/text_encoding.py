"""Выбор кодировки текста источника без внешних детекторов.

Порядок повторяет то, чему на практике можно доверять: BOM однозначен; charset из
Content-Type принимается, только если байты им декодируются без ошибок, потому что
серверы нередко объявляют utf-8 для страниц в cp1251; meta charset читается из начала
HTML; затем строгий UTF-8; запасная кодировка из настроек закрывает старые порталы,
которые кодировку не объявляют вовсе.
"""

from __future__ import annotations

import codecs
import re
from typing import Final

# Старые внутренние порталы на Windows чаще всего отдают cp1251 без объявления.
DEFAULT_FALLBACK_ENCODING: Final = "cp1251"

_BOMS: Final = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# HTML требует объявлять meta charset в первых 1024 байтах документа.
_META_WINDOW: Final = 1024
_META_CHARSET: Final = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.I)


def _strict(raw: bytes, encoding: str) -> str | None:
    try:
        codecs.lookup(encoding)
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return None


def meta_charset(raw: bytes) -> str | None:
    """Кодировка из <meta charset> или http-equiv в начале документа."""
    match = _META_CHARSET.search(raw[:_META_WINDOW])
    return match.group(1).decode("ascii").lower() if match else None


def decode_text(
    raw: bytes, declared: str | None, *, fallback: str, sniff_meta: bool = False
) -> str:
    """Декодирует байты по цепочке BOM, заголовок, meta, UTF-8, запасная кодировка."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    if declared and (text := _strict(raw, declared)) is not None:
        return text
    if sniff_meta and (meta := meta_charset(raw)) and (text := _strict(raw, meta)) is not None:
        return text
    if (text := _strict(raw, "utf-8")) is not None:
        return text
    try:
        return raw.decode(fallback, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
