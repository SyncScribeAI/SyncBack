"""Правила переноса прошлого результата в инкрементальную задачу."""

from __future__ import annotations

import uuid

from app.domain.baseline_result import BaselineFlag, BaselineSuggestion, EvidenceRef, evidence_refs
from app.domain.interfaces.document_analyzer import ChangeKind
from app.domain.services.analysis_result_mapper import FlagDraft, SuggestionDraft
from app.domain.services.delta_merge import (
    EVIDENCE_REMOVED_REASON,
    audit_diff,
    evidence_changed,
    plan_merge,
)


def _id(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)


def _row(number: int, block: str, *, decided: bool, change: str = "modify") -> BaselineSuggestion:
    return BaselineSuggestion(
        id=_id(number),
        block_ref=block,
        section_ref=f"sec-{block}",
        change_type=change,
        original_text=f"old-{block}",
        suggested_text=f"new-{block}",
        decided=decided,
    )


def _draft(
    block: str, kind: ChangeKind = ChangeKind.MODIFY, suggested: str = ""
) -> SuggestionDraft:
    return SuggestionDraft(
        block_ref=block,
        section_ref=f"sec-{block}",
        change_type=kind,
        original_text=f"old-{block}",
        suggested_text=suggested or f"fresh-{block}",
        rationale=None,
        confidence_score=None,
        position=0,
        start_offset=0,
        end_offset=1,
    )


def _flag(block: str, reason: str = "conflict") -> FlagDraft:
    return FlagDraft(
        block_ref=block,
        section_ref=f"sec-{block}",
        reason=reason,
        original_text="t",
        rationale=None,
        quotes=(),
        position=3,
    )


BLOCKS = {f"b{n}": f"text-{n}" for n in range(1, 8)}


def _plan(**overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "baseline_suggestions": [],
        "baseline_flags": [],
        "rechecked": set(),
        "drafts": [],
        "flags": [],
        "add_on_unchanged": [],
        "changed": set(),
        "blocks": BLOCKS,
    }
    arguments.update(overrides)
    if "add_on_unchanged" not in overrides:
        arguments["add_on_unchanged"] = [False] * len(arguments["drafts"])  # type: ignore[arg-type]
    return plan_merge(**arguments)  # type: ignore[arg-type]


def test_not_rechecked_block_keeps_suggestions_and_flags() -> None:
    plan = _plan(
        baseline_suggestions=[_row(1, "b1", decided=False), _row(2, "b1", decided=True)],
        baseline_flags=[BaselineFlag(_id(3), "b1", "conflict", decided=False)],
    )
    assert plan.copy_suggestions == [_id(1), _id(2)]
    assert plan.copy_flags == [_id(3)]
    assert not plan.stale_suggestions


def test_rechecked_pending_suggestion_is_replaced_by_new_result() -> None:
    plan = _plan(
        baseline_suggestions=[_row(1, "b2", decided=False)],
        baseline_flags=[BaselineFlag(_id(2), "b2", "conflict", decided=False)],
        rechecked={"b2"},
        drafts=[_draft("b2")],
    )
    assert (plan.copy_suggestions, plan.copy_flags) == ([], [])
    assert [draft.block_ref for draft in plan.drafts] == ["b2"]
    assert plan.replaced == 1


def test_rechecked_decision_is_kept_and_marked_stale_when_evidence_changed() -> None:
    plan = _plan(
        baseline_suggestions=[_row(1, "b3", decided=True), _row(2, "b4", decided=True)],
        baseline_flags=[BaselineFlag(_id(3), "b3", "conflict", decided=True)],
        rechecked={"b3", "b4"},
        changed={_id(1)},
        drafts=[_draft("b3")],
    )
    assert plan.copy_suggestions == [_id(1), _id(2)]
    assert plan.stale_suggestions == {_id(1)}
    assert plan.copy_flags == [_id(3)]
    # По блоку есть новая правка: флаг об удалённом основании не нужен.
    assert plan.evidence_flags == 0


def test_new_suggestion_equal_to_rejected_inherits_rejection() -> None:
    plan = _plan(
        baseline_suggestions=[_row(1, "b4", decided=True)],
        rechecked={"b4"},
        drafts=[_draft("b4", suggested="new-b4")],
    )
    assert plan.copy_suggestions == [_id(1)]
    assert plan.drafts == []
    assert plan.inherited == 1


def test_new_suggestion_repeating_carried_pending_is_not_duplicated() -> None:
    # Догоняющая правка в непроверенном блоке повторяет перенесённую.
    plan = _plan(
        baseline_suggestions=[_row(1, "b1", decided=False)],
        rechecked={"b2"},
        drafts=[_draft("b1", suggested="new-b1")],
    )
    assert (plan.copy_suggestions, plan.drafts, plan.inherited) == ([_id(1)], [], 1)


def test_add_relying_only_on_unchanged_segments_is_dropped() -> None:
    plan = _plan(
        rechecked={"b5"},
        drafts=[_draft("b5", ChangeKind.ADD), _draft("b5", ChangeKind.ADD, "other")],
        add_on_unchanged=[True, False],
    )
    assert [draft.suggested_text for draft in plan.drafts] == ["other"]
    assert plan.dropped_add == 1


def test_removed_evidence_without_new_suggestion_creates_flag() -> None:
    plan = _plan(
        baseline_suggestions=[_row(1, "b6", decided=False), _row(2, "b7", decided=True)],
        rechecked={"b6", "b7"},
        changed={_id(1), _id(2)},
    )
    assert plan.copy_suggestions == [_id(2)]
    assert plan.stale_suggestions == {_id(2)}
    assert [(flag.block_ref, flag.reason, flag.original_text) for flag in plan.flags] == [
        ("b6", EVIDENCE_REMOVED_REASON, "text-6"),
        ("b7", EVIDENCE_REMOVED_REASON, "text-7"),
    ]
    assert plan.flags[0].section_ref == "sec-b6"
    assert plan.evidence_flags == 2


def test_new_flag_repeating_kept_flag_is_not_duplicated() -> None:
    plan = _plan(
        baseline_flags=[BaselineFlag(_id(1), "b3", "conflict", decided=True)],
        rechecked={"b3"},
        flags=[_flag("b3"), _flag("b3", "missing_fact")],
    )
    assert plan.copy_flags == [_id(1)]
    assert [flag.reason for flag in plan.flags] == ["missing_fact"]


def test_evidence_changed_checks_baseline_fingerprints() -> None:
    fingerprints = {"s": [11, 22]}
    current = {11}
    assert not evidence_changed([EvidenceRef("s", 0)], fingerprints, current)
    assert evidence_changed([EvidenceRef("s", 0), EvidenceRef("s", 1)], fingerprints, current)
    assert not evidence_changed([EvidenceRef("other", 1)], fingerprints, current)
    assert not evidence_changed([EvidenceRef("s", 5)], fingerprints, current)


def test_evidence_refs_skip_rows_without_place() -> None:
    rows = [
        {"snapshot_id": "s", "segment_index": 2},
        {"snapshot_id": None, "segment_index": 1},
        {"snapshot_id": "s", "segment_index": None},
    ]
    assert evidence_refs(rows) == (EvidenceRef("s", 2),)
    assert evidence_refs(None) == ()


def test_audit_diff_counts_missed_blocks() -> None:
    assert audit_diff({"b1", "b2", "b3", "b4"}, {"b1", "b2", "b3", "b9"}) == {
        "blocks_full": 4,
        "blocks_delta": 4,
        "missed": 1,
        "extra": 1,
        "recall": 0.75,
    }
    assert audit_diff(set(), set())["recall"] == 1.0
