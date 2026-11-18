"""Правка, которую не удалось однозначно привязать, становится скрытым флагом ручной проверки."""

from __future__ import annotations

import uuid

import pytest

from app.domain.baseline_result import BaselineSuggestion
from app.domain.evidence import Quote
from app.domain.interfaces.document_analyzer import (
    AnalysisResult,
    AnalysisStatus,
    BlockPayload,
    ProposedChange,
    ReviewFlag,
)
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.services.analysis_result_mapper import RefusedEdit, map_result
from app.domain.services.delta_merge import EVIDENCE_REMOVED_REASON, plan_merge
from app.domain.value_objects import FlagOriginVO
from app.workers.pipeline.flag_drafts import to_flags

TEXT = "Срок 10 дней, ещё раз 10 дней."
BLOCKS = (
    BlockPayload("p-1", DocumentBlockType.PARAGRAPH, TEXT, editable=True, start_offset=0),
    BlockPayload("p-2", DocumentBlockType.PARAGRAPH, "Оплата картой.", True, start_offset=31),
)
QUOTE = Quote("Решения", "Срок сокращён до 5 дней.")


def _result(*changes: ProposedChange, flags: tuple[ReviewFlag, ...] = ()) -> AnalysisResult:
    return AnalysisResult(status=AnalysisStatus.COMPLETED, suggestions=changes, flags=flags)


def _ambiguous(new_text: str = "5 дней") -> ProposedChange:
    return ProposedChange("modify", "p-1", "10 дней", new_text, rationale="r", quotes=(QUOTE,))


def test_ambiguous_edit_becomes_hidden_flag_with_proposal() -> None:
    mapped = map_result(_result(_ambiguous()), BLOCKS)
    assert mapped.suggestions == []
    assert mapped.dropped == {"ambiguous_old_text": 1}
    [flag] = mapped.flags
    assert (flag.origin, flag.reason, flag.block_ref, flag.original_text) == (
        FlagOriginVO.REFUSED_EDIT,
        "edit_ambiguous_old_text",
        "p-1",
        TEXT,
    )
    assert (flag.rationale, flag.quotes, flag.position) == ("r", (QUOTE,), 0)
    assert flag.proposal == RefusedEdit("modify", "10 дней", "5 дней", "ambiguous_old_text")


def test_add_without_anchor_in_block_keeps_anchor_in_proposal() -> None:
    change = ProposedChange("add", "p-2", None, "Оплата по счёту.", anchor_text="Наличными.")
    [flag] = map_result(_result(change), BLOCKS).flags
    assert flag.reason == "edit_anchor_not_in_block"
    assert flag.proposal == RefusedEdit(
        "add", "Наличными.", "Оплата по счёту.", "anchor_not_in_block"
    )


@pytest.mark.parametrize(
    "change",
    [
        ProposedChange("modify", "p-404", "10 дней", "5 дней"),
        ProposedChange("rewrite", "p-1", "10 дней", "5 дней"),
    ],
)
def test_incomplete_model_answer_is_not_flagged(change: ProposedChange) -> None:
    mapped = map_result(_result(change), BLOCKS)
    assert (mapped.suggestions, mapped.flags) == ([], [])
    assert sum(mapped.dropped.values()) == 1


def test_model_flags_go_first_under_limit() -> None:
    model_flag = ReviewFlag("source_conflict", "p-2", "Оплата картой.")
    mapped = map_result(
        _result(_ambiguous(), _ambiguous("6 дней"), flags=(model_flag,)), BLOCKS, max_flags=2
    )
    assert [(f.origin, f.position) for f in mapped.flags] == [
        (FlagOriginVO.ANALYSIS, 0),
        (FlagOriginVO.REFUSED_EDIT, 1),
    ]
    assert mapped.dropped["refused_flags_over_limit"] == 1


def test_placed_edit_does_not_produce_flag() -> None:
    change = ProposedChange("modify", "p-1", "10 дней", "5 дней", occurrence=2)
    mapped = map_result(_result(change), BLOCKS)
    assert (len(mapped.suggestions), mapped.flags) == (1, [])


def test_refused_flag_does_not_hide_removed_evidence_flag() -> None:
    [refused] = map_result(_result(_ambiguous()), BLOCKS).flags
    row = BaselineSuggestion(
        id=uuid.UUID(int=1),
        block_ref="p-1",
        section_ref="sec",
        change_type="modify",
        original_text="10 дней",
        suggested_text="7 дней",
        decided=False,
    )
    plan = plan_merge(
        baseline_suggestions=[row],
        baseline_flags=[],
        rechecked={"p-1"},
        drafts=[],
        flags=[refused],
        add_on_unchanged=[],
        changed={row.id},
        blocks={"p-1": TEXT},
    )
    assert [flag.reason for flag in plan.flags] == [
        "edit_ambiguous_old_text",
        EVIDENCE_REMOVED_REASON,
    ]
    assert plan.evidence_flags == 1


def test_rows_keep_origin_and_proposal() -> None:
    mapped = map_result(_result(_ambiguous(), flags=(ReviewFlag("x", "p-2", "t"),)), BLOCKS)
    rows = to_flags(mapped.flags, job_id=uuid.uuid4(), document_id=uuid.uuid4())
    assert [(row.origin, row.proposal) for row in rows] == [
        ("analysis", None),
        (
            "refused_edit",
            {
                "kind": "modify",
                "old_text": "10 дней",
                "new_text": "5 дней",
                "refusal": "ambiguous_old_text",
            },
        ),
    ]
