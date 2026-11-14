"""Сведение результата инкрементального анализа с результатом прошлой задачи.

Инкрементальная задача проверяет заново только часть блоков, а показывать пользователь
должен полный результат. Правила переноса:

* блок не перепроверялся: правки и флаги прошлой задачи переносятся со статусами;
* блок перепроверен, прошлая правка ждала решения: её заменяет новый результат;
* блок перепроверен, решение уже принято: правка переносится, а если её основание
  изменилось или исчезло из источника, помечается stale;
* новая правка совпала с перенесённой (блок, тип, старый и новый текст): остаётся
  перенесённая, вместе с решением, поэтому отклонённая правка не показывается снова;
* новая правка add опирается только на неизменённые сегменты: прошлый полный анализ
  видел те же факты и правку не предложил, это шум выборочного запроса;
* основание прошлой правки исчезло, а новой правки по блоку нет: создаётся флаг
  «проверьте вручную».

План строится в памяти по сводкам строк, сами строки копирует репозиторий. Время и
память линейны по числу правок и флагов обеих задач.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.domain.baseline_result import BaselineFlag, BaselineSuggestion, EvidenceRef
from app.domain.interfaces.document_analyzer import ChangeKind
from app.domain.services.analysis_result_mapper import FlagDraft, SuggestionDraft

EVIDENCE_REMOVED_REASON = "evidence_removed"
EVIDENCE_REMOVED_TEXT = "Основание правки удалено из источника, проверьте вручную"


def evidence_changed(
    refs: Iterable[EvidenceRef],
    baseline_fingerprints: Mapping[str, Sequence[int]],
    current: Collection[int],
) -> bool:
    """Есть ли среди оснований сегмент, которого нет в текущем входе.

    Основание без найденного места или со снимком, отпечатки которого не прочитались,
    изменением не считается: без отпечатка о нём ничего не известно, а ложная пометка
    stale заставила бы пересматривать решения, которые остались верными.
    """
    for ref in refs:
        fingerprints = baseline_fingerprints.get(ref.snapshot_id)
        if fingerprints is None or not 0 <= ref.segment_index < len(fingerprints):
            continue
        if fingerprints[ref.segment_index] not in current:
            return True
    return False


@dataclass(slots=True)
class MergePlan:
    """Что перенести из прошлой задачи и что записать из нового ответа."""

    copy_suggestions: list[uuid.UUID] = field(default_factory=list)
    stale_suggestions: set[uuid.UUID] = field(default_factory=set)
    copy_flags: list[uuid.UUID] = field(default_factory=list)
    drafts: list[SuggestionDraft] = field(default_factory=list)
    flags: list[FlagDraft] = field(default_factory=list)
    replaced: int = 0
    inherited: int = 0
    dropped_add: int = 0
    evidence_flags: int = 0

    def as_meta(self) -> dict[str, int]:
        return {
            "carried_suggestions": len(self.copy_suggestions),
            "carried_flags": len(self.copy_flags),
            "stale": len(self.stale_suggestions),
            "replaced": self.replaced,
            "inherited": self.inherited,
            "dropped_add": self.dropped_add,
            "evidence_flags": self.evidence_flags,
        }


def _key(
    block_ref: str | None, change_type: str, original: str | None, suggested: str | None
) -> tuple[str | None, str, str, str]:
    return block_ref, change_type, original or "", suggested or ""


def plan_merge(
    *,
    baseline_suggestions: Iterable[BaselineSuggestion],
    baseline_flags: Iterable[BaselineFlag],
    rechecked: Collection[str],
    drafts: Sequence[SuggestionDraft],
    flags: Sequence[FlagDraft],
    add_on_unchanged: Sequence[bool],
    changed: Collection[uuid.UUID],
    blocks: Mapping[str, str],
) -> MergePlan:
    """add_on_unchanged параллелен drafts; changed: id прошлых правок с изменённым основанием.

    blocks: текст блоков документа по ref, из него берётся текст флага об удалённом
    основании.
    """
    plan = MergePlan()
    kept_keys: set[tuple[str | None, str, str, str]] = set()
    lost_evidence: dict[str, str] = {}
    for row in baseline_suggestions:
        if row.block_ref is not None and row.block_ref in rechecked and not row.decided:
            plan.replaced += 1
            if row.id in changed:
                lost_evidence.setdefault(row.block_ref, row.section_ref)
            continue
        plan.copy_suggestions.append(row.id)
        kept_keys.add(_key(row.block_ref, row.change_type, row.original_text, row.suggested_text))
        if row.block_ref is not None and row.block_ref in rechecked and row.id in changed:
            plan.stale_suggestions.add(row.id)
            lost_evidence.setdefault(row.block_ref, row.section_ref)

    kept_flags: set[tuple[str, str]] = set()
    for row_flag in baseline_flags:
        if row_flag.block_ref in rechecked and not row_flag.decided:
            continue
        plan.copy_flags.append(row_flag.id)
        kept_flags.add((row_flag.block_ref, row_flag.reason))

    answered: set[str] = set()
    for draft, on_unchanged in zip(drafts, add_on_unchanged, strict=True):
        key = _key(
            draft.block_ref, draft.change_type.value, draft.original_text, draft.suggested_text
        )
        if key in kept_keys:
            plan.inherited += 1
            answered.add(draft.block_ref)
            continue
        if draft.change_type is ChangeKind.ADD and on_unchanged:
            plan.dropped_add += 1
            continue
        plan.drafts.append(draft)
        answered.add(draft.block_ref)

    position = max((flag.position for flag in flags), default=-1) + 1
    for flag in flags:
        if (flag.block_ref, flag.reason) in kept_flags:
            continue
        plan.flags.append(flag)
        answered.add(flag.block_ref)
    for block_ref, section_ref in lost_evidence.items():
        text = blocks.get(block_ref)
        if block_ref in answered or text is None:
            continue
        if (block_ref, EVIDENCE_REMOVED_REASON) in kept_flags:
            continue
        plan.flags.append(
            FlagDraft(
                block_ref=block_ref,
                section_ref=section_ref,
                reason=EVIDENCE_REMOVED_REASON,
                original_text=text,
                rationale=EVIDENCE_REMOVED_TEXT,
                quotes=(),
                position=position,
            )
        )
        position += 1
        plan.evidence_flags += 1
    return plan


def audit_diff(full_blocks: Collection[str], delta_blocks: Collection[str]) -> dict[str, float]:
    """Расхождение выборочной задачи с контрольным полным анализом по блокам.

    Блок считается найденным, если у него есть правка или флаг. recall: доля блоков
    полного анализа, которые нашла и выборочная задача.
    """
    full = set(full_blocks)
    delta = set(delta_blocks)
    found = len(full & delta)
    return {
        "blocks_full": len(full),
        "blocks_delta": len(delta),
        "missed": len(full - delta),
        "extra": len(delta - full),
        "recall": round(found / len(full), 4) if full else 1.0,
    }
