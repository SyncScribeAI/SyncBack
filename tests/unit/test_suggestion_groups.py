"""Группы правок одной фразы и догоняющие правки: правила без БД и инфраструктуры."""

import time
import uuid

import pytest

from app.domain.interfaces.document_analyzer import (
    AnalysisResult,
    AnalysisStatus,
    BlockPayload,
    ChangeKind,
    ProposedChange,
)
from app.domain.services.analysis_request_builder import DocumentInput, build_analysis_request
from app.domain.services.analysis_result_mapper import SuggestionDraft, map_result
from app.domain.services.suggestion_groups import (
    GROUP_KEY_LENGTH,
    PropagationLimits,
    assign_group_keys,
    finalize_drafts,
    group_key,
    normalize_phrase,
    propagate_exact,
)
from app.domain.value_objects import SuggestionOriginVO
from app.infrastructure.parsers.markdown_parser import MarkdownParser

MD = (
    "## Сроки\n\n"
    "Ответ — 10 дней. Повтор — 10 дней.\n\n"
    "Возврат — 10 дней.\n\n"
    "```\nwait = '10 дней'\n```\n\n"
    "## Прочее\n\n"
    "Хранение — 10 дней.\n"
)
LIMITS = PropagationLimits(max_groups=50, max_per_group=50)


def _blocks(text: str = MD) -> tuple[BlockPayload, ...]:
    parsed = MarkdownParser().parse(text.encode())
    document = DocumentInput(
        id=uuid.uuid4(), name="spec.md", format="md", plain_text=text, blocks=parsed.blocks
    )
    request = build_analysis_request(job_id=uuid.uuid4(), attempt=0, document=document, sources=[])
    return request.document.blocks


def _ref(blocks: tuple[BlockPayload, ...], text: str) -> str:
    return next(block.ref for block in blocks if text in block.text)


def _drafts(blocks: tuple[BlockPayload, ...], *changes: ProposedChange) -> list[SuggestionDraft]:
    result = AnalysisResult(status=AnalysisStatus.COMPLETED, suggestions=changes)
    return map_result(result, blocks).suggestions


def _ten_days(ref: str, new_text: str = "5 дней") -> ProposedChange:
    return ProposedChange("modify", ref, "10 дней", new_text, rationale="Решение встречи")


# --- group_key ----------------------------------------------------------------------


def test_normalized_phrase_ignores_case_spaces_and_yo() -> None:
    assert normalize_phrase("  Ещё\u00a0 ДЕСЯТЬ\tдней ") == "еще десять дней"


def test_group_key_joins_cosmetic_variants_only() -> None:
    base = group_key(ChangeKind.MODIFY, "10 дней", "5 дней")
    assert len(base) == GROUP_KEY_LENGTH
    assert group_key(ChangeKind.MODIFY, "10  ДНЕЙ", "5 Дней") == base
    # Другие слова, другой новый текст или тип правки — другая группа.
    assert group_key(ChangeKind.MODIFY, "десяти дней", "5 дней") != base
    assert group_key(ChangeKind.MODIFY, "10 дней", "7 дней") != base
    assert group_key(ChangeKind.DELETE, "10 дней", None) != base
    # Разделитель не даёт склеить поля: «a|bc» и «ab|c» не совпадают.
    assert group_key(ChangeKind.MODIFY, "a", "bc") != group_key(ChangeKind.MODIFY, "ab", "c")


def test_single_change_has_no_group_and_pairs_share_key() -> None:
    blocks = _blocks()
    first, second = _ref(blocks, "Ответ"), _ref(blocks, "Возврат")
    drafts = _drafts(
        blocks, _ten_days(first), _ten_days(second), _ten_days(first, new_text="7 дней")
    )
    keyed = assign_group_keys(drafts)
    assert keyed[0].group_key is not None
    assert keyed[0].group_key == keyed[1].group_key
    assert keyed[2].group_key is None
    assert [d.position for d in keyed] == [0, 1, 2]


# --- догоняющие правки ----------------------------------------------------------------


def test_propagation_covers_other_exact_repeats_in_editable_blocks() -> None:
    blocks = _blocks()
    model = _drafts(blocks, _ten_days(_ref(blocks, "Возврат")))
    extra = propagate_exact(model, blocks, LIMITS)

    # Код — только контекст: повтор внутри него не трогаем.
    code = next(block for block in blocks if not block.editable and "wait" in block.text)
    assert all(d.block_ref != code.ref for d in extra)
    assert [(d.block_ref, d.section_ref) for d in extra] == [
        (_ref(blocks, "Ответ"), "Сроки"),
        (_ref(blocks, "Ответ"), "Сроки"),
        (_ref(blocks, "Хранение"), "Прочее"),
    ]
    assert [d.position for d in extra] == [1, 2, 3]
    assert {d.origin for d in extra} == {SuggestionOriginVO.PROPAGATED}
    assert all(d.suggested_text == "5 дней" for d in extra)
    assert all(d.rationale and d.rationale.startswith("Решение встречи\n\n") for d in extra)
    assert all("Найдено автоматически" in (d.rationale or "") for d in extra)
    text = MD
    assert all(text[d.start_offset : d.end_offset] == "10 дней" for d in extra)


def test_propagation_skips_places_already_changed_by_model() -> None:
    blocks = _blocks()
    first = _ref(blocks, "Ответ")
    # Модель уже исправила оба вхождения в первом абзаце, одно — другим текстом.
    model = _drafts(blocks, _ten_days(first), _ten_days(first, new_text="7 дней"))
    extra = propagate_exact(model, blocks, LIMITS)
    occupied = {(d.start_offset, d.end_offset) for d in model}
    assert not occupied & {(d.start_offset, d.end_offset) for d in extra}
    # Две группы, но свободных мест только два: второй группе ничего не досталось.
    assert len(extra) == 2
    assert {d.suggested_text for d in extra} == {"5 дней"}


def test_additions_are_not_propagated() -> None:
    blocks = _blocks()
    model = _drafts(
        blocks,
        ProposedChange(
            "add", _ref(blocks, "Возврат"), None, "Можно продлить.", anchor_text="10 дней"
        ),
    )
    assert propagate_exact(model, blocks, LIMITS) == []


def test_propagation_respects_limits() -> None:
    blocks = _blocks()
    model = _drafts(
        blocks,
        _ten_days(_ref(blocks, "Возврат")),
        ProposedChange("modify", _ref(blocks, "Хранение"), "Хранение", "Срок хранения"),
    )
    extra = propagate_exact(model, blocks, PropagationLimits(max_groups=1, max_per_group=2))
    assert len(extra) == 2
    assert {d.original_text for d in extra} == {"10 дней"}


@pytest.mark.parametrize(("groups", "per_group"), [(0, 1), (1, 0)])
def test_limits_must_be_positive(groups: int, per_group: int) -> None:
    with pytest.raises(ValueError):
        PropagationLimits(max_groups=groups, max_per_group=per_group)


def test_finalize_groups_model_change_with_its_propagated_copies() -> None:
    blocks = _blocks()
    model = _drafts(blocks, _ten_days(_ref(blocks, "Возврат")))

    alone = finalize_drafts(model, blocks, None)
    assert [(d.origin, d.group_key) for d in alone] == [(SuggestionOriginVO.MODEL, None)]

    combined = finalize_drafts(model, blocks, LIMITS)
    assert len(combined) == 4
    assert len({d.group_key for d in combined}) == 1
    assert combined[0].group_key is not None


@pytest.mark.perf
def test_grouping_and_propagation_scale_linearly() -> None:
    def run(scale: int) -> float:
        text = "".join(f"Пункт {i}: срок 10 дней.\n\n" for i in range(2000 * scale))
        blocks = _blocks(text)
        changes = [_ten_days(blocks[i].ref) for i in range(0, 500 * scale)]
        drafts = _drafts(blocks, *changes)
        started = time.perf_counter()
        finalize_drafts(drafts, blocks, LIMITS)
        return time.perf_counter() - started

    small = min(run(1) for _ in range(3))
    large = min(run(4) for _ in range(3))
    # Одна группа и лимит на неё: время растёт с текстом и числом правок, а не с их произведением.
    assert large < small * 8
