"""
Применяет принятые правки к docx, сохраняя оформление абзаца: текст меняется внутри ранов
(docx_runs), а не переписыванием абзаца одним раном.

Абзацы перебираются в том же порядке, что и в DocxParser (тело и ячейки таблиц), и по
этому обходу восстанавливаются их начала в plain_text (абзацы склеены через «\\n»).
Правка с границами попадает в свой абзац бинарным поиском, если текст под границами
совпадает с old_text; остальные правки ищут первое вхождение в исходном тексте абзацев.
Оба пути сводятся к правке абзаца в его нормализованных координатах и дальше идут через
одну функцию замены. Итого O(P + R + S log S): P абзацев, R ранов затронутых абзацев,
S правок.

Добавление (add) вставляется только по якорю — новым абзацем сразу после абзаца с первым
вхождением old_text, как в TextExporter. Якоря нет или он не найден — правка не применяется;
место вставки за модель не выбираем.

Если атомы абзаца не сопоставляются с его текстом или правка задевает табуляцию или
перенос строки, абзац переписывается прежним способом одним раном. Правка в результате
поля не применяется. После сохранения файл разбирается заново и сверяется с ожидаемым
текстом; отчёт с числом правок по способам применения пишется в лог.
"""

from __future__ import annotations

import io
import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from docx import Document as DocxDocument
from docx.text.paragraph import Paragraph

from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.text_normalization import normalize_text
from app.infrastructure.docx_paragraphs import iter_docx_paragraphs
from app.infrastructure.exporters.docx_runs import (
    EditOutcome,
    Unmappable,
    apply_in_runs,
    classify,
    insert_paragraph_after,
    paragraph_atoms,
    rewrite_paragraph,
)
from app.infrastructure.exporters.text_edits import TextEdit, apply_edits

logger = logging.getLogger("syncscribe.exporters.docx")


@dataclass(slots=True)
class DocxExportReport:
    in_runs: int = 0
    rewritten: int = 0
    unapplied: int = 0
    reasons: list[str] = field(default_factory=list)
    # None — сверку не делали (правок нет), False — повторный разбор дал другой текст.
    verified: bool | None = None


class _ParagraphIndex:
    """Абзацы документа, их нормализованный текст и начала в plain_text парсера."""

    __slots__ = ("paragraphs", "starts", "texts")

    def __init__(self, document: Any) -> None:
        self.paragraphs: list[Paragraph] = []
        self.texts: list[str] = []
        self.starts: list[int] = []
        offset = 0
        for item in iter_docx_paragraphs(document):
            text = normalize_text(item.paragraph.text)
            self.paragraphs.append(item.paragraph)
            self.texts.append(text)
            self.starts.append(offset)
            offset += len(text) + 1

    def locate(self, change: AppliedChange) -> tuple[int, int, int] | None:
        """(номер абзаца, начало, конец) old_text по границам правки или None."""
        if not change.has_span or not self.starts:
            return None
        start, end = change.start_offset or 0, change.end_offset or 0
        index = bisect_right(self.starts, start) - 1
        if index < 0:
            return None
        local_start = start - self.starts[index]
        local_end = end - self.starts[index]
        text = self.texts[index]
        old_text = normalize_text(change.old_text or "")
        if not old_text.strip() or local_end > len(text):
            return None
        if text[local_start:local_end] != old_text:
            return None
        return index, local_start, local_end

    def search(self, needle: str) -> tuple[int, int, int] | None:
        """Первое вхождение в порядке обхода парсера; O(T) по тексту документа."""
        for number, text in enumerate(self.texts):
            position = text.find(needle)
            if position >= 0:
                return number, position, position + len(needle)
        return None


@dataclass(slots=True)
class _Plan:
    edits: dict[int, list[TextEdit]] = field(default_factory=dict)
    additions: dict[int, list[str]] = field(default_factory=dict)
    unapplied: int = 0


def _plan(index: _ParagraphIndex, changes: list[AppliedChange]) -> _Plan:
    plan = _Plan()
    for change in changes:
        if change.change_type == "add":
            anchor = normalize_text(change.old_text or "").strip()
            located = index.locate(change) or (index.search(anchor) if anchor else None)
            if located is None or not change.new_text:
                plan.unapplied += 1
                continue
            plan.additions.setdefault(located[0], []).append(change.new_text)
            continue
        old_text = normalize_text(change.old_text or "")
        if change.change_type not in ("modify", "delete") or not old_text:
            plan.unapplied += 1
            continue
        located = index.locate(change) or index.search(old_text)
        if located is None:
            plan.unapplied += 1
            continue
        number, start, end = located
        replacement = change.new_text or "" if change.change_type == "modify" else ""
        plan.edits.setdefault(number, []).append(TextEdit(start, end, replacement=replacement))
    return plan


def _non_overlapping(edits: list[TextEdit]) -> tuple[list[TextEdit], int]:
    kept: list[TextEdit] = []
    cursor = 0
    for edit in sorted(edits, key=lambda e: e.start):
        if edit.start < cursor:
            continue
        kept.append(edit)
        cursor = edit.end
    return kept, len(edits) - len(kept)


def _apply_paragraph(
    paragraph: Paragraph, text: str, edits: list[TextEdit], report: DocxExportReport
) -> str:
    """Применить правки абзаца и вернуть его ожидаемый нормализованный текст."""
    edits, skipped = _non_overlapping(edits)
    report.unapplied += skipped
    try:
        atoms = paragraph_atoms(paragraph)
    except Unmappable as exc:
        report.rewritten += len(edits)
        report.reasons.append(str(exc))
        new_text = apply_edits(text, edits)
        rewrite_paragraph(paragraph, new_text)
        return new_text
    outcomes = [classify(atoms, edit) for edit in edits]
    applicable = [e for e, o in zip(edits, outcomes, strict=True) if o is not EditOutcome.FIELD]
    fields = len(edits) - len(applicable)
    if fields:
        report.unapplied += fields
        report.reasons.append("правка в результате поля")
    new_text = apply_edits(text, applicable)
    if EditOutcome.REWRITE in outcomes:
        report.rewritten += len(applicable)
        report.reasons.append("правка задевает табуляцию или перенос строки")
        rewrite_paragraph(paragraph, new_text)
    else:
        apply_in_runs(atoms, applicable)
        report.in_runs += len(applicable)
    return new_text


class DocxExporter:
    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes:
        result, _ = self.apply_changes_with_report(raw_bytes, changes)
        return result

    def apply_changes_with_report(
        self, raw_bytes: bytes, changes: list[AppliedChange]
    ) -> tuple[bytes, DocxExportReport]:
        report = DocxExportReport()
        if not changes:
            return raw_bytes, report
        document = DocxDocument(io.BytesIO(raw_bytes))
        index = _ParagraphIndex(document)
        plan = _plan(index, changes)
        report.unapplied += plan.unapplied
        expected = list(index.texts)
        for number, edits in plan.edits.items():
            expected[number] = _apply_paragraph(
                index.paragraphs[number], index.texts[number], edits, report
            )
        inserted: dict[int, list[str]] = {}
        for number, texts in plan.additions.items():
            # addnext ставит абзац сразу за якорем, поэтому вставка с конца сохраняет порядок.
            for text in reversed(texts):
                insert_paragraph_after(index.paragraphs[number], text)
            inserted[number] = [normalize_text(text) for text in texts]
            report.in_runs += len(texts)
        buffer = io.BytesIO()
        document.save(buffer)
        output = buffer.getvalue()
        report.verified = _verify(output, expected, inserted, report)
        logger.info(
            "Экспорт docx",
            extra={
                "in_runs": report.in_runs,
                "rewritten": report.rewritten,
                "unapplied": report.unapplied,
                "verified": report.verified,
            },
        )
        return output, report


def _verify(
    output: bytes, expected: list[str], inserted: dict[int, list[str]], report: DocxExportReport
) -> bool:
    """Повторный разбор результата обязан дать исходный текст с теми же правками."""
    parts: list[str] = []
    for number, text in enumerate(expected):
        parts.append(text)
        parts.extend(inserted.get(number, ()))
    document = DocxDocument(io.BytesIO(output))
    actual = [normalize_text(item.paragraph.text) for item in iter_docx_paragraphs(document)]
    if actual == parts:
        return True
    report.reasons.append("повторный разбор дал другой текст")
    logger.warning("Экспорт docx: результат расходится с ожидаемым текстом")
    return False
