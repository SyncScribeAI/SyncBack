"""Перевод ответа LLM в ORM-правки анализа.

Модель оформляет правки не всегда так, как просит промпт, поэтому перед сохранением
ответ нормализуется:
- modify с пустым new_text — это delete;
- modify, где new_text начинается с old_text, — это добавление: модель «дописала»
  предложение, повторив абзац. Такая правка становится add с якорем в последнем абзаце
  old_text и new_text без повторённой части — иначе для docx она была бы отброшена
  как многоабзацная, а для md/txt заменила абзац сама собой;
- якорь добавления берётся из insert_after (или old_text для старых ответов); если его нет,
  а раздел section_ref есть в документе, якорем становится первый абзац раздела, и это
  явно отмечается в rationale — ревьюер видит, что место выбрано автоматически;
- modify/delete сохраняются только с непустым old_text, который дословно присутствует
  в документе: иначе экспорт всё равно не сможет применить такую правку;
- для docx фрагмент с переводом строки отбрасывается: экспорт ищет old_text внутри
  одного абзаца и такую правку всё равно не применит.
Якорь добавления сохраняется в original_text — так его ждут экспортёры и фронтенд.

Логи не содержат текста документа и ответа модели: только идентификаторы, счётчики и
причины отброса, чтобы данные пользователя не попадали в внешнюю систему логирования.
"""

import logging
import uuid
from dataclasses import replace

from app.domain.interfaces.llm_client import LLMSuggestionBatch, LLMSuggestionItem
from app.infrastructure.db.models.enums import ChangeType, SuggestionStatus
from app.infrastructure.db.models.suggestion import Suggestion
from app.workers.pipeline.suggestion_diagnostics import (
    MAX_RECORDS,
    DiagnosticBudget,
    log_skip,
    safe_id,
)

logger = logging.getLogger("syncscribe.workers.suggestion_mapper")

_CHANGE_TYPE_MAP = {"add": ChangeType.ADD, "modify": ChangeType.MODIFY, "delete": ChangeType.DELETE}
_SINGLE_PARAGRAPH_FORMATS = frozenset({"docx", "doc"})


def _heading_text(line: str) -> str:
    """Текст заголовка без markdown-решёток и выделения: '## **Auth**' → 'Auth'."""
    return line.strip().lstrip("#").strip().strip("*").strip()


def infer_anchor(document_text: str, section_ref: str) -> str | None:
    """Якорь для добавления без якоря: первый абзац раздела section_ref.

    Если раздел есть, но абзацев после заголовка нет — сам заголовок.
    None, если заголовка с таким текстом в документе нет.
    """
    wanted = _heading_text(section_ref or "")
    if not wanted:
        return None
    lines = document_text.split("\n")
    for index, line in enumerate(lines):
        if _heading_text(line) != wanted:
            continue
        for candidate in lines[index + 1 :]:
            if candidate.strip():
                return candidate.strip()
        return line.strip()
    return None


def _last_paragraph(text: str) -> str:
    paragraphs = [line.strip() for line in text.split("\n") if line.strip()]
    return paragraphs[-1] if paragraphs else ""


def _normalize(item: LLMSuggestionItem) -> LLMSuggestionItem:
    """Привести modify к тому, чем он является на самом деле.

    modify без нового текста — удаление: модель часто оформляет delete именно так.
    modify, где новый текст начинается со старого, — добавление хвоста после old_text.
    """
    if item.change_type != "modify":
        return item
    old = (item.old_text or "").strip()
    new = (item.new_text or "").strip()
    if old and not new:
        return replace(item, change_type="delete", new_text=None)
    if old and len(new) > len(old) and new.startswith(old):
        return replace(
            item,
            change_type="add",
            old_text="",
            new_text=new[len(old) :].strip(),
            insert_after=_last_paragraph(old),
        )
    return item


def _resolve_anchor(item: LLMSuggestionItem, document_text: str) -> tuple[LLMSuggestionItem, bool]:
    """Для add положить якорь в old_text; второй элемент — выбран ли он автоматически."""
    if item.change_type != "add":
        return item, False
    anchor = (item.insert_after or item.old_text or "").strip()
    if anchor:
        return replace(item, old_text=anchor, insert_after=None), False
    inferred = infer_anchor(document_text, item.section_ref)
    return replace(item, old_text=inferred, insert_after=None), inferred is not None


def _skip_reason(
    item: LLMSuggestionItem, document_text: str, *, single_paragraph: bool
) -> str | None:
    """Почему правку нельзя сохранить; None — если можно."""
    if item.change_type not in _CHANGE_TYPE_MAP:
        return "unknown_change_type"
    old_text = (item.old_text or "").strip()
    if item.change_type == "add":
        if not old_text:
            return "add_without_anchor"
        if old_text not in document_text:
            return "add_anchor_not_in_document"
        return None
    if not old_text:
        return "old_text_missing"
    if single_paragraph and "\n" in old_text:
        return "old_text_spans_paragraphs"
    if old_text not in document_text:
        return "old_text_not_in_document"
    return None


def _with_inferred_note(rationale: str | None, section_ref: str) -> str:
    note = f"Место вставки выбрано автоматически: после первого абзаца раздела «{section_ref}»."
    if rationale and rationale.strip():
        return f"{rationale.rstrip()} {note}"
    return note


def _log_skipped(
    batch: LLMSuggestionBatch,
    skipped_items: list[tuple[str, LLMSuggestionItem]],
    *,
    kept: int,
    analysis_job_id: uuid.UUID,
    document_id: uuid.UUID,
    source_id: uuid.UUID | None,
    diagnostic_records: int,
) -> None:
    """Агрегированная диагностика отброшенных правок без текста документа и ответа модели.

    Причины отброса — в тексте сообщения: консольный форматтер воркера не печатает extra.
    Тексты old_text/new_text, section_ref и сырой ответ модели в логи не попадают:
    они могут содержать фрагменты пользовательского документа или источников.
    """
    skipped: dict[str, int] = {}
    for reason, _ in skipped_items:
        skipped[reason] = skipped.get(reason, 0) + 1
    context = {
        "analysis_job_id": safe_id(analysis_job_id),
        "document_id": safe_id(document_id),
        "source_id": safe_id(source_id),
        "received": len(batch.items),
        "dropped": len(skipped_items),
        "diagnostic_records": diagnostic_records,
        "diagnostic_records_omitted": len(skipped_items) - diagnostic_records,
        "skipped": skipped,
        "kept": kept,
    }
    details = ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items()))
    logger.warning(
        "Часть правок LLM отброшена как невалидные: %s (сохранено %d из %d) "
        "received=%d kept=%d dropped=%d diagnostic_records=%d diagnostic_records_omitted=%d "
        "(analysis_job_id=%s document_id=%s source_id=%s)",
        details,
        kept,
        len(batch.items),
        len(batch.items),
        kept,
        len(skipped_items),
        diagnostic_records,
        len(skipped_items) - diagnostic_records,
        context["analysis_job_id"],
        context["document_id"],
        context["source_id"],
        extra=context,
    )
    if kept == 0:
        logger.warning(
            "Все %d правок LLM отброшены (job=%s, document=%s)",
            len(batch.items),
            context["analysis_job_id"],
            context["document_id"],
            extra=context,
        )


def map_to_suggestions(
    batch: LLMSuggestionBatch,
    analysis_job_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    document_text: str,
    document_format: str = "",
    source_id: uuid.UUID | None = None,
    diagnostics_enabled: bool = True,
) -> list[Suggestion]:
    """Невалидные элементы пропускаются с предупреждением в логе: лучше потерять одну правку,
    чем уронить обработку источника целиком."""
    single_paragraph = document_format.lower() in _SINGLE_PARAGRAPH_FORMATS
    suggestions: list[Suggestion] = []
    skipped_items: list[tuple[str, LLMSuggestionItem]] = []
    diagnostic_records = 0
    budget = DiagnosticBudget()
    inferred_count = 0
    appended_count = 0
    for index, raw_item in enumerate(batch.items):
        normalized = _normalize(raw_item)
        if raw_item.change_type == "modify" and normalized.change_type == "add":
            appended_count += 1
        item, inferred = _resolve_anchor(normalized, document_text)
        reason = _skip_reason(item, document_text, single_paragraph=single_paragraph)
        if reason is not None:
            skipped_items.append((reason, item))
            if diagnostics_enabled and diagnostic_records < MAX_RECORDS:
                log_skip(
                    logger,
                    index=index,
                    reason=reason,
                    raw=raw_item,
                    normalized=normalized,
                    resolved=item,
                    inferred=inferred,
                    document_text=document_text,
                    analysis_job_id=analysis_job_id,
                    document_id=document_id,
                    source_id=source_id,
                    budget=budget,
                )
                diagnostic_records += 1
            continue
        rationale = item.rationale
        if inferred:
            inferred_count += 1
            rationale = _with_inferred_note(rationale, item.section_ref)
        suggestions.append(
            Suggestion(
                analysis_job_id=analysis_job_id,
                document_id=document_id,
                section_ref=item.section_ref,
                change_type=_CHANGE_TYPE_MAP[item.change_type],
                status=SuggestionStatus.PENDING,
                original_text=item.old_text,
                suggested_text=item.new_text,
                rationale=rationale,
                confidence_score=item.confidence_score,
            )
        )
    log_context = {"analysis_job_id": safe_id(analysis_job_id), "document_id": safe_id(document_id)}
    if appended_count:
        logger.info(
            "Для %d правок modify с дописанным текстом тип изменён на add",
            appended_count,
            extra=log_context,
        )
    if inferred_count:
        logger.info(
            "Для %d добавлений якорь выбран автоматически (первый абзац раздела)",
            inferred_count,
            extra=log_context,
        )
    if skipped_items:
        _log_skipped(
            batch,
            skipped_items,
            kept=len(suggestions),
            analysis_job_id=analysis_job_id,
            document_id=document_id,
            source_id=source_id,
            diagnostic_records=diagnostic_records,
        )
    return suggestions
