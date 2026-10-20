"""Безопасная диагностика: почему цитата old_text не найдена в тексте документа.

Только диагностика: результат не влияет на сохранение правок и не применяется для поиска
позиции. Наружу отдаются класс расхождения, длины и коды Unicode-символов, но не текст.
"""

import re
import unicodedata
from dataclasses import dataclass

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_SPACE_VARIANTS = {ord(char): " " for char in "\u00a0\u202f\u2007\u2009"}
_PROMPT_TAG_LIKE = re.compile(r"<\s*/?\s*(document|source)\b", re.IGNORECASE)
_MAX_CODEPOINTS = 8


@dataclass(frozen=True)
class QuoteDiagnosis:
    category: str
    quote_len: int
    matched_prefix_len: int
    suspicious_codepoints: tuple[str, ...]


def _technical(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_SPACE_VARIANTS).translate(_INVISIBLE)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _matched_prefix_len(quote: str, document_text: str) -> int:
    low, high = 0, len(quote)
    while low < high:
        middle = (low + high + 1) // 2
        if quote[:middle] in document_text:
            low = middle
        else:
            high = middle - 1
    return low


def _suspicious_codepoints(quote: str) -> tuple[str, ...]:
    found: set[str] = set()
    for char in quote:
        if char in (" ", "\n"):
            continue
        if unicodedata.category(char) in {"Cc", "Cf", "Zs", "Zl", "Zp"}:
            found.add(f"U+{ord(char):04X}")
    return tuple(sorted(found))[:_MAX_CODEPOINTS]


def _category(quote: str, document_text: str) -> str:
    if quote in document_text:
        return "present"
    if "\r" in quote and quote.replace("\r\n", "\n").replace("\r", "\n") in document_text:
        return "line_endings"
    nfc = unicodedata.normalize("NFC", quote)
    if nfc != quote and nfc in document_text:
        return "unicode_normalization"
    technical_quote = _technical(quote)
    if technical_quote in _technical(document_text):
        return "invisible_or_space_variants"
    if _collapse(technical_quote) in _collapse(_technical(document_text)):
        return "whitespace_runs"
    if technical_quote.casefold() in _technical(document_text).casefold():
        return "letter_case"
    if _PROMPT_TAG_LIKE.search(quote):
        return "prompt_tag_like_text"
    return "not_found"


def diagnose_quote(old_text: str, document_text: str) -> QuoteDiagnosis:
    quote = (old_text or "").strip()
    return QuoteDiagnosis(
        category=_category(quote, document_text),
        quote_len=len(quote),
        matched_prefix_len=_matched_prefix_len(quote, document_text),
        suspicious_codepoints=_suspicious_codepoints(quote),
    )
