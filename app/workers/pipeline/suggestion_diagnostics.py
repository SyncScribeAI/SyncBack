"""Bounded, metadata-only diagnostics. Never used to accept or locate a suggestion.

A category means a transformed substring matched, not a proven incident cause.
No content, hashes, character codes, offsets or nearest-fragment search are emitted.
"""

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from itertools import takewhile

from app.domain.interfaces.llm_client import LLMSuggestionItem

MAX_RECORDS = 20
MAX_DOCUMENT_CHARS = 200_000
MAX_QUOTE_CHARS = 8_192
MAX_TRANSFORM_CHARS = 2_000_000
_SPACE = str.maketrans({ord(char): " " for char in "\u00a0\u202f\u2007\u2009"})
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\ufeff\u00ad\u2060"), None)
# Mirrors only the delimiter-prefix escaping in prompt._escape; not HTML unescaping.
_ESCAPED_TAG = re.compile(r"&lt;(/?)(document|source)\b", re.IGNORECASE)
_REASONS = frozenset(
    {
        "unknown_change_type",
        "add_without_anchor",
        "add_anchor_not_in_document",
        "old_text_missing",
        "old_text_spans_paragraphs",
        "old_text_not_in_document",
    }
)
_MISMATCHES = frozenset({"add_anchor_not_in_document", "old_text_not_in_document"})


def safe_id(value: uuid.UUID | None) -> str:
    return str(value) if type(value) is uuid.UUID else "unavailable"


def safe_change_type(value: str) -> str:
    return value if type(value) is str and value in {"add", "modify", "delete"} else "unknown"


def _length(value: str | None) -> int:
    return len(value) if isinstance(value, str) else 0


def _stripped_length(value: str) -> int:
    # No extra full-size allocation, even when classification is input-limited.
    leading = sum(1 for _ in takewhile(str.isspace, value))
    if leading == len(value):
        return 0
    trailing = sum(1 for _ in takewhile(str.isspace, reversed(value)))
    return len(value) - leading - trailing


def _line_endings(value: str) -> str:
    return value.replace("\r\n", "\n")


def _spaces(value: str) -> str:
    return value.translate(_SPACE)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _tabs(value: str) -> str:
    return value.replace("\t", " ")


def _invisibles(value: str) -> str:
    return value.translate(_INVISIBLE)


def _prompt_delimiters(value: str) -> str:
    return _ESCAPED_TAG.sub(lambda match: f"<{match.group(1)}{match.group(2)}", value)


_TRANSFORMS = (
    ("space_variants", _spaces),
    ("line_endings", _line_endings),
    ("unicode_nfc", _nfc),
    ("tabs", _tabs),
    ("invisible_chars", _invisibles),
    ("case_only", str.casefold),
    ("prompt_delimiter_escape", _prompt_delimiters),
)


@dataclass
class DiagnosticBudget:
    remaining_chars: int = MAX_TRANSFORM_CHARS

    def classify(self, quote: str, document: str) -> str:
        if len(quote) > MAX_QUOTE_CHARS or len(document) > MAX_DOCUMENT_CHARS:
            return "input_limit"
        quote = quote.strip()
        if not quote:
            return "not_applicable"
        for category, transform in _TRANSFORMS:
            cost = len(quote) + len(document)
            if cost > self.remaining_chars:
                return "budget_limit"
            self.remaining_chars -= cost
            candidate = transform(quote)
            transformed_document = transform(document)
            if not candidate:
                continue
            start = transformed_document.find(candidate)
            if start < 0:
                continue
            # At most two candidates, including overlapping matches. No position escapes.
            if transformed_document.find(candidate, start + 1) >= 0:
                return "ambiguous"
            return category
        return "unknown"


def anchor_origin(raw: LLMSuggestionItem, normalized: LLMSuggestionItem, *, inferred: bool) -> str:
    if normalized.change_type != "add":
        return "not_applicable"
    if inferred:
        return "inferred_section"
    if raw.change_type == "modify":
        return "normalized_modify_tail"
    if normalized.insert_after:
        return "insert_after"
    if normalized.old_text:
        return "legacy_old_text"
    return "missing"


def log_skip(
    logger: logging.Logger,
    *,
    index: int,
    reason: str,
    raw: LLMSuggestionItem,
    normalized: LLMSuggestionItem,
    resolved: LLMSuggestionItem,
    inferred: bool,
    document_text: str,
    analysis_job_id: uuid.UUID,
    document_id: uuid.UUID,
    source_id: uuid.UUID | None,
    budget: DiagnosticBudget,
) -> None:
    """Build only allowlisted scalars; identical metadata in console and JSON."""
    checked = resolved.old_text or ""
    category = "not_applicable"
    if reason in _MISMATCHES:
        try:
            category = budget.classify(checked, document_text)
        except Exception:
            # Never serialize an exception: it may include either input text.
            category = "diagnostic_error"
    origin = anchor_origin(raw, normalized, inferred=inferred)
    if origin == "insert_after":
        anchor_len = _length(raw.insert_after)
    elif origin == "legacy_old_text":
        anchor_len = _length(raw.old_text)
    elif origin == "normalized_modify_tail":
        anchor_len = _length(normalized.insert_after)
    elif origin == "inferred_section":
        anchor_len = _length(resolved.old_text)
    else:
        anchor_len = 0
    metadata = {
        "analysis_job_id": safe_id(analysis_job_id),
        "document_id": safe_id(document_id),
        "source_id": safe_id(source_id),
        "item_index_0based": index,
        "original_change_type": safe_change_type(raw.change_type),
        "final_change_type": safe_change_type(resolved.change_type),
        "reason": reason if reason in _REASONS else "unknown",
        "raw_old_text_len": _length(raw.old_text),
        "raw_insert_after_len": _length(raw.insert_after),
        "anchor_origin": origin,
        "anchor_len": anchor_len,
        "checked_old_text_len": _stripped_length(checked),
        "document_len": len(document_text),
        "mismatch_category": category,
    }
    logger.warning(
        "LLM suggestion skipped: %s",
        " ".join(f"{key}={value}" for key, value in metadata.items()),
        extra=metadata,
    )
