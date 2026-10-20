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
Для отброшенных правок с текстовым несовпадением дополнительно пишутся метаданные
расхождения (индекс, длины, позиция, класс различия, коды Unicode) — тоже без текста.
Диагностика только классифицирует причину и никогда не принимает правку.
"""

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, replace

from app.domain.interfaces.llm_client import LLMSuggestionBatch, LLMSuggestionItem
from app.infrastructure.db.models.enums import ChangeType, SuggestionStatus
from app.infrastructure.db.models.suggestion import Suggestion

logger = logging.getLogger("syncscribe.workers.suggestion_mapper")

_CHANGE_TYPE_MAP = {"add": ChangeType.ADD, "modify": ChangeType.MODIFY, "delete": ChangeType.DELETE}
_SINGLE_PARAGRAPH_FORMATS = frozenset({"docx", "doc"})

_TEXT_MISMATCH_REASONS = frozenset({"old_text_not_in_document", "add_anchor_not_in_document"})
_MAX_DIAGNOSTIC_LOGS = 20
_MAX_PREFIX_CANDIDATES = 200
_SPACE_VARIANTS_RE = re.compile("[\u00a0\u202f\u2007\u2009]")
_INVISIBLE_RE = re.compile("[\u200b\ufeff\u00ad\u2060]")


@dataclass(frozen=True)
class SkipDiagnostic:
    """Метаданные расхождения без текста документа и ответа модели."""

    index: int
    reason: str
    original_change_type: str
    final_change_type: str
    text_len: int
    document_len: int
    common_prefix_len: int
    technical_class: str
    text_char: str | None
    document_char: str | None


def _codepoint(char: str | None) -> str | None:
    return None if char is None else f"U+{ord(char):04X}"


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def technical_mismatch_class(text: str, document_text: str) -> str:
    """Классифицировать техническое различие; на принятие правки это не влияет.

    Проверяется один вид преобразования за раз: если после него фрагмент находится
    в документе, различие техническое. no_technical_match — преобразования не помогли
    (перефразирование, другая цитата, комбинация различий).
    """
    variants = (
        ("line_endings", text.replace("\r\n", "\n").replace("\r", "\n")),
        ("unicode_nfc", unicodedata.normalize("NFC", text)),
        ("space_variants", _SPACE_VARIANTS_RE.sub(" ", text)),
        ("invisible_chars", _INVISIBLE_RE.sub("", text)),
    )
    for name, variant in variants:
        candidate = variant.strip()
        if variant != text and candidate and candidate in document_text:
            return name
    collapsed = _collapse_whitespace(text)
    if collapsed and collapsed in _collapse_whitespace(document_text):
        return "whitespace_runs"
    folded = text.casefold()
    if folded and folded in document_text.casefold():
        return "case_only"
    return "no_technical_match"


def first_divergence(text: str, document_text: str) -> tuple[int, str | None, str | None]:
    """Лучшее частичное совпадение: (длина общего префикса, символ текста, символ документа).

    Возвращаются только одиночные символы для перевода в коды Unicode — не строки.
    """
    if not text:
        return 0, None, None
    best_len = 0
    best_start = -1
    start = document_text.find(text[0])
    candidates = 0
    while start != -1 and candidates < _MAX_PREFIX_CANDIDATES:
        size = 0
        while (
            size < len(text)
            and start + size < len(document_text)
            and text[size] == document_text[start + size]
        ):
            size += 1
        if size > best_len or best_start == -1:
            best_len = size
            best_start = start
        candidates += 1
        start = document_text.find(text[0], start + 1)
    text_char = text[best_len] if best_len < len(text) else None
    if best_start == -1:
        return 0, text_char, None
    doc_pos = best_start + best_len
    document_char = document_text[doc_pos] if doc_pos < len(document_text) else None
    return best_len, text_char, document_char


def _build_diagnostic(
    index: int,
    reason: str,
    raw_item: LLMSuggestionItem,
    item: LLMSuggestionItem,
    document_text: str,
) -> SkipDiagnostic:
    text = (item.old_text or "").strip()
    prefix_len, text_char, document_char = first_divergence(text, document_text)
    return SkipDiagnostic(
        index=index,
        reason=reason,
        original_change_type=raw_item.change_type,
        final_change_type=item.change_type,
        text_len=len(text),
        document_len=len(document_text),
        common_prefix_len=prefix_len,
        technical_class=technical_mismatch_class(text, document_text),
        text_char=text_char,
        document_char=document_char,
    )


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
        "analysis_job_id": str(analysis_job_id),
        "document_id": str(document_id),
        "skipped": skipped,
        "kept": kept,
    }
    details = ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items()))
    logger.warning(
        "Часть правок LLM отброшена как невалидные: %s (сохранено %d из %d)",
        details,
        kept,
        len(batch.items),
        extra=context,
    )
    if kept == 0:
        logger.warning(
            "Все %d правок LLM отброшены (job=%s, document=%s)",
            len(batch.items),
            analysis_job_id,
            document_id,
            extra=context,
        )


def _log_skip_diagnostics(
    diagnostics: list[SkipDiagnostic],
    *,
    analysis_job_id: uuid.UUID,
    document_id: uuid.UUID,
) -> None:
    """По одной строке на отброшенную правку: метаданные расхождения без текста.

    Пишутся индекс, типы, длины, позиция, класс технического различия и коды Unicode
    первых различающихся символов. Сами тексты и цитаты в лог не попадают.
    """
    for diagnostic in diagnostics[:_MAX_DIAGNOSTIC_LOGS]:
        logger.warning(
            "Правка LLM отброшена: item=%d reason=%s type=%s->%s text_len=%d doc_len=%d "
            "common_prefix=%d class=%s text_char=%s doc_char=%s (job=%s, document=%s)",
            diagnostic.index,
            diagnostic.reason,
            diagnostic.original_change_type,
            diagnostic.final_change_type,
            diagnostic.text_len,
            diagnostic.document_len,
            diagnostic.common_prefix_len,
            diagnostic.technical_class,
            _codepoint(diagnostic.text_char) or "-",
            _codepoint(diagnostic.document_char) or "-",
            analysis_job_id,
            document_id,
        )
    hidden = len(diagnostics) - _MAX_DIAGNOSTIC_LOGS
    if hidden > 0:
        logger.warning("Диагностика ещё %d отброшенных правок не выведена (лимит)", hidden)


def map_to_suggestions(
    batch: LLMSuggestionBatch,
    analysis_job_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    document_text: str,
    document_format: str = "",
) -> list[Suggestion]:
    """Невалидные элементы пропускаются с предупреждением в логе: лучше потерять одну правку,
    чем уронить обработку источника целиком."""
    single_paragraph = document_format.lower() in _SINGLE_PARAGRAPH_FORMATS
    suggestions: list[Suggestion] = []
    skipped_items: list[tuple[str, LLMSuggestionItem]] = []
    diagnostics: list[SkipDiagnostic] = []
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
            if reason in _TEXT_MISMATCH_REASONS:
                diagnostics.append(_build_diagnostic(index, reason, raw_item, item, document_text))
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
    log_context = {"analysis_job_id": str(analysis_job_id), "document_id": str(document_id)}
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
        )
        _log_skip_diagnostics(
            diagnostics,
            analysis_job_id=analysis_job_id,
            document_id=document_id,
        )
    return suggestions
