"""Диагностика отброшенных правок: метаданные расхождения без текста."""

import logging
import uuid

from app.domain.interfaces.llm_client import LLMSuggestionBatch, LLMSuggestionItem
from app.workers.pipeline.suggestion_mapper import (
    first_divergence,
    map_to_suggestions,
    technical_mismatch_class,
)

LOGGER_NAME = "syncscribe.workers.suggestion_mapper"
DOCUMENT = "Backup is enabled\nsecond paragraph"


def _item(change_type: str, old_text: str | None, **kwargs) -> LLMSuggestionItem:
    return LLMSuggestionItem(
        section_ref="section",
        change_type=change_type,
        old_text=old_text,
        new_text=kwargs.pop("new_text", "replacement"),
        **kwargs,
    )


def test_class_space_variants():
    assert technical_mismatch_class("Backup\u00a0is enabled", DOCUMENT) == "space_variants"


def test_class_line_endings():
    assert technical_mismatch_class("line one\r\nline two", "line one\nline two") == "line_endings"


def test_class_unicode_nfc():
    assert technical_mismatch_class("cafe\u0301 menu", "caf\u00e9 menu") == "unicode_nfc"


def test_class_invisible_chars():
    assert technical_mismatch_class("abc\u200bdef", "abcdef") == "invisible_chars"


def test_class_whitespace_runs():
    assert technical_mismatch_class("Backup  is\tenabled", DOCUMENT) == "whitespace_runs"


def test_class_case_only():
    assert technical_mismatch_class("backup IS enabled", DOCUMENT) == "case_only"


def test_class_paraphrase_is_not_technical():
    assert technical_mismatch_class("Backups are turned on", DOCUMENT) == "no_technical_match"


def test_first_divergence_reports_codepoints_source():
    prefix_len, text_char, document_char = first_divergence("Backup\u00a0is enabled", DOCUMENT)
    assert prefix_len == len("Backup")
    assert text_char == "\u00a0"
    assert document_char == " "


def test_first_divergence_when_first_char_absent():
    assert first_divergence("xyz", "abc") == (0, "x", None)


def test_first_divergence_empty_text():
    assert first_divergence("", DOCUMENT) == (0, None, None)


def test_mapper_keeps_valid_and_skips_mismatches_unchanged():
    batch = LLMSuggestionBatch(
        items=[
            _item("modify", "Backup is enabled", new_text="Backup is on"),
            _item("modify", "Backup\u00a0is enabled", new_text="Backup is on"),
            _item("add", None, insert_after="missing anchor"),
        ]
    )
    result = map_to_suggestions(
        batch, uuid.uuid4(), uuid.uuid4(), document_text=DOCUMENT, document_format="txt"
    )
    assert len(result) == 1


def test_diagnostics_logged_without_text(caplog):
    batch = LLMSuggestionBatch(
        items=[
            _item("modify", "Backup is enabled", new_text="Backup is on"),
            _item("modify", "Backup\u00a0is enabled", new_text="Backup is on"),
        ]
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        map_to_suggestions(
            batch, uuid.uuid4(), uuid.uuid4(), document_text=DOCUMENT, document_format="txt"
        )
    messages = "\n".join(caplog.messages)
    assert "item=1" in messages
    assert "reason=old_text_not_in_document" in messages
    assert "class=space_variants" in messages
    assert "text_char=U+00A0" in messages
    assert "doc_char=U+0020" in messages
    assert "Backup" not in messages
    assert "enabled" not in messages
