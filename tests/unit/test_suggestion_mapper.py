"""Маппер: якорь добавления из insert_after или первого абзаца раздела, нормализация
modify→delete и modify→add, валидация исходного фрагмента, отброс многоабзацных
фрагментов для docx и причины отброса в тексте лога."""

import logging
import uuid

import pytest

from app.domain.interfaces.llm_client import LLMSuggestionBatch, LLMSuggestionItem
from app.infrastructure.db.models.enums import ChangeType
from app.workers.pipeline.suggestion_mapper import infer_anchor, map_to_suggestions

DOCUMENT = "# Сроки\nСрок ответа — 10 дней.\nОплата картой.\n"
LOGGER = "syncscribe.workers.suggestion_mapper"


def _item(
    change_type: str,
    old_text: str | None,
    new_text: str | None = "Новый текст.",
    *,
    section: str = "Сроки",
    insert_after: str | None = None,
    rationale: str | None = None,
) -> LLMSuggestionItem:
    return LLMSuggestionItem(
        section_ref=section,
        change_type=change_type,
        old_text=old_text,
        new_text=new_text,
        rationale=rationale,
        confidence_score=0.9,
        insert_after=insert_after,
    )


def _map(
    *items: LLMSuggestionItem,
    document: str = DOCUMENT,
    document_format: str = "md",
    raw_response: str = "{}",
):
    batch = LLMSuggestionBatch(items=list(items), raw_response=raw_response)
    return map_to_suggestions(
        batch,
        uuid.uuid4(),
        uuid.uuid4(),
        document_text=document,
        document_format=document_format,
    )


# ── якорь добавления ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_add_with_insert_after_is_kept_and_anchor_stored_in_original_text() -> None:
    result = _map(_item("add", "", insert_after="Оплата картой.", rationale="Источник."))
    assert len(result) == 1
    assert result[0].change_type is ChangeType.ADD
    assert result[0].original_text == "Оплата картой."
    assert result[0].rationale == "Источник."


def test_add_with_anchor_in_old_text_is_still_accepted() -> None:
    result = _map(_item("add", "Срок ответа — 10 дней."))
    assert len(result) == 1
    assert result[0].original_text == "Срок ответа — 10 дней."


def test_insert_after_wins_over_old_text() -> None:
    result = _map(_item("add", "Срок ответа — 10 дней.", insert_after="Оплата картой."))
    assert result[0].original_text == "Оплата картой."


@pytest.mark.parametrize("anchor", [None, "", "   "])
def test_add_without_anchor_infers_first_paragraph_of_section(anchor: str | None) -> None:
    result = _map(_item("add", anchor, rationale="Источник: новое правило."))
    assert len(result) == 1
    assert result[0].original_text == "Срок ответа — 10 дней."
    assert result[0].rationale == (
        "Источник: новое правило. Место вставки выбрано автоматически: "
        "после первого абзаца раздела «Сроки»."
    )


def test_inferred_anchor_note_without_rationale() -> None:
    result = _map(_item("add", None))
    assert result[0].rationale.startswith("Место вставки выбрано автоматически")


def test_add_without_anchor_and_unknown_section_is_dropped() -> None:
    assert _map(_item("add", None, section="Нет такого раздела")) == []


def test_add_with_anchor_missing_from_document_is_dropped() -> None:
    assert _map(_item("add", "", insert_after="Такого абзаца нет.")) == []


def test_anchor_is_matched_after_strip() -> None:
    assert len(_map(_item("add", "", insert_after="  Оплата картой.\n"))) == 1


# ── infer_anchor ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_infer_anchor_handles_markdown_and_plain_headings() -> None:
    assert infer_anchor(DOCUMENT, "Сроки") == "Срок ответа — 10 дней."
    docx_like = "Support Service\n\nЕдиная служба поддержки.\nТаблица tickets\n"
    assert infer_anchor(docx_like, "Support Service") == "Единая служба поддержки."
    assert infer_anchor("## **Auth**\nАбзац.\n", "Auth") == "Абзац."


def test_infer_anchor_falls_back_to_heading_and_none() -> None:
    assert infer_anchor("# Пустой раздел\n", "Пустой раздел") == "# Пустой раздел"
    assert infer_anchor(DOCUMENT, "Нет такого") is None
    assert infer_anchor(DOCUMENT, "") is None


# ── modify / delete ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_modify_with_empty_new_text_becomes_delete() -> None:
    result = _map(_item("modify", "Оплата картой.", ""), _item("modify", "Оплата картой.", None))
    assert [s.change_type for s in result] == [ChangeType.DELETE, ChangeType.DELETE]
    assert all(s.suggested_text is None for s in result)


@pytest.mark.parametrize("change_type", ["modify", "delete"])
def test_modify_and_delete_without_old_text_are_dropped(change_type: str) -> None:
    assert _map(_item(change_type, None, None), _item(change_type, "   ", None)) == []


@pytest.mark.parametrize("change_type", ["modify", "delete"])
def test_modify_and_delete_with_old_text_missing_from_document_are_dropped(
    change_type: str,
) -> None:
    assert _map(_item(change_type, "Нет в документе", None)) == []


def test_modify_and_delete_with_existing_old_text_are_kept() -> None:
    result = _map(
        _item("modify", "Срок ответа — 10 дней.", "Срок ответа — 5 дней."),
        _item("delete", "Оплата картой.", None),
    )
    assert [s.change_type for s in result] == [ChangeType.MODIFY, ChangeType.DELETE]


def test_old_text_spanning_paragraphs_is_dropped_only_for_docx() -> None:
    spanning = _item("modify", "Срок ответа — 10 дней.\nОплата картой.", "Один абзац.")
    assert _map(spanning, document_format="docx") == []
    assert _map(spanning, document_format="DOCX") == []
    assert len(_map(spanning, document_format="md")) == 1
    assert len(_map(spanning, document_format="txt")) == 1


def test_unknown_change_type_is_still_dropped() -> None:
    result = _map(_item("rename", "Оплата картой."), _item("add", "Оплата картой."))
    assert [s.change_type for s in result] == [ChangeType.ADD]


# ── modify с дописыванием → add ────────────────────────────────────────────────────────────────────────────────────────────────


def test_modify_that_appends_text_becomes_add_after_old_text() -> None:
    result = _map(_item("modify", "Оплата картой.", "Оплата картой. Возможна оплата по счёту."))
    assert len(result) == 1
    assert result[0].change_type is ChangeType.ADD
    assert result[0].original_text == "Оплата картой."
    assert result[0].suggested_text == "Возможна оплата по счёту."


def test_appending_modify_spanning_paragraphs_is_rescued_for_docx() -> None:
    old = "Срок ответа — 10 дней.\nОплата картой."
    result = _map(_item("modify", old, old + "\nНовое предложение."), document_format="docx")
    assert len(result) == 1
    assert result[0].change_type is ChangeType.ADD
    assert result[0].original_text == "Оплата картой."
    assert result[0].suggested_text == "Новое предложение."


def test_modify_with_real_replacement_stays_modify() -> None:
    result = _map(_item("modify", "Оплата картой.", "Оплата картой или по счёту."))
    assert result[0].change_type is ChangeType.MODIFY
    assert result[0].suggested_text == "Оплата картой или по счёту."


def test_modify_identical_to_old_text_is_not_converted() -> None:
    result = _map(_item("modify", "Оплата картой.", "Оплата картой."))
    assert result[0].change_type is ChangeType.MODIFY


def test_appended_modifies_are_counted_in_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _map(_item("modify", "Оплата картой.", "Оплата картой. Ещё."))
    assert any(
        "Для 1 правок modify с дописанным текстом тип изменён на add" in r.getMessage()
        for r in caplog.records
    )


# ── логи ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_skip_reasons_are_in_log_message_without_user_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = _map(
            _item("add", "", insert_after="Оплата картой."),
            _item("add", "", insert_after="такого текста нет"),
            _item("add", None, section="Нет такого"),
        )
    assert len(result) == 1
    messages = [record.getMessage() for record in caplog.records]
    summary = next(m for m in messages if m.startswith("Часть правок LLM отброшена"))
    assert "add_anchor_not_in_document=1" in summary
    assert "add_without_anchor=1" in summary
    assert "сохранено 1 из 3" in summary
    assert not any("такого текста нет" in m for m in messages)
    assert not any("Сырой ответ" in m for m in messages)


def test_missing_original_text_reasons_are_logged_without_user_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = "Секретный фрагмент, которого нет в документе"
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = _map(
            _item("modify", missing, "Замена"),
            _item("delete", None, None),
            _item("modify", "Оплата картой.", "Оплата по счёту."),
        )
    assert len(result) == 1
    messages = [record.getMessage() for record in caplog.records]
    summary = next(m for m in messages if m.startswith("Часть правок LLM отброшена"))
    assert "old_text_missing=1" in summary
    assert "old_text_not_in_document=1" in summary
    assert "сохранено 1 из 3" in summary
    assert not any(missing in message for message in messages)


def test_raw_response_is_never_logged_when_everything_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = '{"suggestions": [{"change_type": "add", "insert_after": "нет"}]}'
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = _map(_item("add", "", insert_after="такого текста нет"), raw_response=raw)
    assert result == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("Все 1 правок LLM отброшены") for m in messages)
    assert not any(raw in m for m in messages)
    assert not any("такого текста нет" in m for m in messages)


def test_inferred_anchors_are_counted_in_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _map(_item("add", None), _item("add", None))
    assert any(
        "Для 2 добавлений якорь выбран автоматически" in record.getMessage()
        for record in caplog.records
    )


def test_position_is_index_in_model_answer_including_dropped_items() -> None:
    result = _map(
        _item("modify", "Оплата картой.", "Оплата по счёту."),
        _item("modify", "нет в документе", "x"),
        _item("modify", "10 дней", "5 дней"),
    )
    assert [(s.original_text, s.position) for s in result] == [
        ("Оплата картой.", 0),
        ("10 дней", 2),
    ]
    # Прежний конвейер работает с текстом без блоков: привязки к блоку нет.
    assert {(s.block_id, s.start_offset, s.end_offset) for s in result} == {(None, None, None)}
