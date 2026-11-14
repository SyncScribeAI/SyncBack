"""Выбор блоков и сегментов источника для инкрементального анализа."""

from __future__ import annotations

import uuid

from app.domain.analysis_reuse import Baseline, DeltaFallback, DeltaPolicy
from app.domain.baseline_result import BaselineFlag, BaselineSuggestion, EvidenceRef
from app.domain.evidence import Evidence, EvidenceMatch
from app.domain.interfaces.document_analyzer import ChangeKind, SourceKind, SourcePayload
from app.domain.interfaces.document_parser import DocumentBlockType, ParsedBlock
from app.domain.services.analysis_request_builder import DocumentInput
from app.domain.services.analysis_result_mapper import SuggestionDraft
from app.domain.services.delta_request import (
    BaselineFingerprints,
    DeltaRequest,
    add_on_unchanged,
    build_delta_request,
    checkable_blocks,
)
from app.domain.services.delta_selection import (
    BlockText,
    DeltaLimits,
    SourceInput,
    TermIndex,
    reduce_sources,
    select_blocks,
    text_terms,
)
from app.domain.source_snapshot import SegmentKind, SourceSegment, segment_fingerprints

BLOCKS = [
    BlockText("b1", "Срок ответа на обращение составляет пять рабочих дней"),
    BlockText("b2", "Оплата производится банковской картой"),
    BlockText("b3", "Версия протокола 2.4.1 поддерживается сервером"),
    BlockText("b4", "Сотрудник оформляет заявку в системе учёта"),
]
STRICT = DeltaLimits(blocks_per_segment=1, score_threshold=0.99, rare_block_count=0)


def _select(changed: list[str], limits: DeltaLimits = STRICT, **groups: set[str]) -> set[str]:
    selected = select_blocks(
        BLOCKS,
        changed,
        evidence_changed=groups.get("evidence", set()),
        flagged=groups.get("flagged", set()),
        limits=limits,
    )
    return set(selected.refs)


def test_terms_keep_numbers_whole_and_cut_words_to_stem() -> None:
    terms = text_terms("Серверы и сервера версии 2.4.1 от 01.02.2026", 6)
    assert terms.strong == {"2.4.1", "01.02.2026"}
    assert terms.counts["сервер"] == 2
    assert "и" not in terms.counts


def test_overlap_group_picks_best_block_for_changed_segment() -> None:
    selected = select_blocks(
        BLOCKS,
        ["Срок ответа на обращение составляет три рабочих дня"],
        evidence_changed=(),
        flagged=(),
        limits=STRICT,
    )
    assert selected.refs == {"b1"}
    assert (selected.by_overlap, selected.by_token) == (1, 0)


def test_overlap_threshold_adds_every_close_block() -> None:
    limits = DeltaLimits(blocks_per_segment=1, score_threshold=0.05, rare_block_count=0)
    assert _select(["оформляет заявку, оплату картой"], limits) == {"b2", "b4"}


def test_token_group_adds_block_sharing_version_number() -> None:
    selected = select_blocks(
        BLOCKS,
        ["Срок ответа на обращение составляет пять дней, протокол 2.4.1"],
        evidence_changed=(),
        flagged=(),
        limits=STRICT,
    )
    assert selected.refs == {"b1", "b3"}
    assert selected.by_token == 1


def test_token_group_adds_block_sharing_rare_word() -> None:
    limits = DeltaLimits(blocks_per_segment=1, score_threshold=0.99, rare_block_count=1)
    # «учёта» есть только в b4; лучший по перекрытию блок b1.
    assert _select(["Срок ответа на обращение составляет пять рабочих дней учёта"], limits) == {
        "b1",
        "b4",
    }


def test_evidence_and_flag_groups_add_known_blocks_only() -> None:
    selected = select_blocks(
        BLOCKS,
        [],
        evidence_changed={"b4", "missing"},
        flagged={"b2", "b4"},
        limits=STRICT,
    )
    assert selected.refs == {"b2", "b4"}
    assert (selected.by_evidence, selected.by_flag) == (1, 1)


def test_common_words_do_not_select_blocks() -> None:
    blocks = [BlockText(f"b{n}", f"система номер{chr(1072 + n)}") for n in range(25)]
    index = TermIndex(text_terms(block.text, 6) for block in blocks)
    assert index.is_common("систем")
    selected = select_blocks(
        blocks,
        ["система"],
        evidence_changed=(),
        flagged=(),
        limits=DeltaLimits(rare_block_count=2),
    )
    assert selected.refs == frozenset()


SOURCE = (
    "# Раздел\n\nальфа один\n\nбета два\n\nгамма три\n\n"
    "# Второй\n\nдельта четыре\n\nэпсилон зарплата\n\nдзета шесть"
)


def test_reduce_sources_sends_changed_with_context_support_and_headings() -> None:
    segments = segment_fingerprints(SOURCE)
    assert [segment.kind for segment, _ in segments][:2] == [
        SegmentKind.HEADING,
        SegmentKind.PARAGRAPH,
    ]
    base = {fp for position, (_, fp) in enumerate(segments) if position != 2}
    [reduced] = reduce_sources(
        [SourceInput(text=SOURCE, segments=segments)],
        base,
        ["оклад и зарплата сотрудника"],
        DeltaLimits(context_segments=1, support_segments=1),
    )
    assert reduced.segment_indexes == (0, 1, 2, 3, 4, 6)
    assert reduced.text == (
        "# Раздел\n\nальфа один\n\nбета два\n\nгамма три\n\n# Второй\n\nэпсилон зарплата"
    )


def test_reduce_sources_leaves_untouched_source_empty() -> None:
    segments = segment_fingerprints(SOURCE)
    [reduced] = reduce_sources(
        [SourceInput(text=SOURCE, segments=segments)],
        {fp for _, fp in segments},
        [],
        DeltaLimits(),
    )
    assert (reduced.segment_indexes, reduced.text) == ((), "")


def _document(texts: list[str]) -> DocumentInput:
    blocks: list[ParsedBlock] = []
    plain = ""
    for position, text in enumerate(texts):
        if plain:
            plain += "\n\n"
        start = len(plain)
        plain += text
        kind = (
            DocumentBlockType.HEADING if text.startswith("Раздел") else DocumentBlockType.PARAGRAPH
        )
        blocks.append(
            ParsedBlock(
                ref=f"b{position}",
                type=kind,
                position=position,
                start_offset=start,
                end_offset=len(plain),
                editable=True,
            )
        )
    return DocumentInput(
        id=uuid.UUID(int=1), name="doc", format="markdown", plain_text=plain, blocks=blocks
    )


DOCUMENT = _document(
    [
        "Раздел оплаты",
        "Срок ответа составляет пять рабочих дней",
        "Оплата производится банковской картой",
        "Заявка оформляется в системе учёта",
        "Отчёт формируется ежемесячно бухгалтерией",
    ]
)
BASELINE = Baseline(job_id=uuid.UUID(int=9), input_hash="b" * 64, document_hash="d" * 64)


def _payload(text: str) -> SourcePayload:
    return SourcePayload(id="s", name="Регламент", type=SourceKind.FILE, text=text)


def _build(
    old: str, new: str, policy: DeltaPolicy = DeltaPolicy(), **rows: object
) -> DeltaRequest | DeltaFallback:
    base_segments = segment_fingerprints(old)
    segments = segment_fingerprints(new)
    return build_delta_request(
        baseline=BASELINE,
        fingerprints=BaselineFingerprints(
            all=frozenset(fp for _, fp in base_segments),
            by_snapshot={"old": [fp for _, fp in base_segments]},
        ),
        document=DOCUMENT,
        payloads=[_payload(new)],
        segments=[segments],
        suggestions=rows.get("suggestions", []),  # type: ignore[arg-type]
        flags=rows.get("flags", []),  # type: ignore[arg-type]
        policy=policy,
        limits=DeltaLimits(blocks_per_segment=1, score_threshold=0.99, support_segments=1),
    )


OLD = "\n\n".join(
    [
        "# Регламент",
        "Срок ответа составляет пять рабочих дней.",
        "Оплата банковской картой.",
        "Заявки ведутся в системе учёта.",
        "Отчёт готовит бухгалтерия.",
        "Офис открыт с девяти.",
        "Парковка для гостей.",
        "Пропуск выдаёт охрана.",
        "Обед с часу до двух.",
        "Связь по телефону.",
        "Почта для писем.",
    ]
)


def test_checkable_blocks_skip_headings() -> None:
    assert [block.ref for block in checkable_blocks(DOCUMENT)] == ["b1", "b2", "b3", "b4"]


def test_delta_request_selects_block_and_reduces_source() -> None:
    new = OLD.replace("пять рабочих дней", "три рабочих дня")
    planned = _build(OLD, new)
    assert isinstance(planned, DeltaRequest)
    assert planned.selected.refs == {"b1"}
    [source] = planned.sources
    assert "три рабочих дня" in source.text
    assert "Почта для писем." not in source.text
    assert planned.stats.blocks_checked == 1
    assert planned.stats.segments_sent < planned.stats.segments_total
    assert planned.stats.chars_sent == len(source.text)
    assert (planned.summary.segments_added, planned.summary.segments_removed) == (1, 1)


def test_delta_request_falls_back_when_share_is_large() -> None:
    new = OLD.replace("пять рабочих дней", "три рабочих дня")
    assert _build(OLD, new, DeltaPolicy(max_changed_share=0.01)) is DeltaFallback.CHANGED_SHARE


def test_delta_request_falls_back_when_many_blocks_selected() -> None:
    new = OLD.replace("пять рабочих дней", "три рабочих дня").replace("картой", "переводом")
    assert _build(OLD, new, DeltaPolicy(max_blocks_share=0.25)) is DeltaFallback.BLOCKS_SHARE


def test_delta_request_falls_back_when_new_text_matches_no_block() -> None:
    new = OLD + "\n\nКофемашина стоит у окна."
    assert _build(OLD, new) is DeltaFallback.NO_BLOCKS_MATCHED


def test_delta_request_rechecks_block_whose_evidence_was_removed() -> None:
    old_segments = segment_fingerprints(OLD)
    removed = next(
        position
        for position, (segment, _) in enumerate(old_segments)
        if "Отчёт" in segment.text_in(OLD)
    )
    new = OLD.replace("\n\nОтчёт готовит бухгалтерия.", "")
    suggestion = BaselineSuggestion(
        id=uuid.UUID(int=5),
        block_ref="b4",
        section_ref="s",
        change_type="modify",
        original_text="ежемесячно",
        suggested_text="ежеквартально",
        decided=True,
        evidence=(EvidenceRef("old", removed),),
    )
    planned = _build(OLD, new, suggestions=[suggestion])
    assert isinstance(planned, DeltaRequest)
    assert planned.selected.refs == {"b4"}
    assert planned.selected.by_evidence == 1
    assert planned.changed == {suggestion.id}
    # Опоры для блока в оставшемся тексте нет: вызывать модель не с чем.
    assert planned.sources == []
    assert planned.rechecked == {"b4"}


def test_delta_request_without_changes_selects_nothing() -> None:
    new = OLD.replace("\n\nПочта для писем.", "")
    planned = _build(OLD, new)
    assert isinstance(planned, DeltaRequest)
    assert (planned.selected.refs, planned.sources) == (frozenset(), [])


def test_flagged_block_is_rechecked() -> None:
    new = OLD.replace("пять рабочих дней", "три рабочих дня")
    flag = BaselineFlag(id=uuid.UUID(int=6), block_ref="b3", reason="conflict", decided=False)
    planned = _build(OLD, new, flags=[flag])
    assert isinstance(planned, DeltaRequest)
    assert planned.selected.refs == {"b1", "b3"}
    assert planned.rechecked == planned.selected.refs


def test_flagged_block_keeps_flag_when_model_is_not_called() -> None:
    # У b4 нет опоры в оставшемся тексте, поэтому модель не вызывается.
    new = OLD.replace("\n\nОтчёт готовит бухгалтерия.", "")
    flag = BaselineFlag(id=uuid.UUID(int=6), block_ref="b4", reason="conflict", decided=False)
    planned = _build(OLD, new, flags=[flag])
    assert isinstance(planned, DeltaRequest)
    assert not planned.calls_model
    assert planned.rechecked == frozenset()


def _draft(kind: ChangeKind, evidence: tuple[Evidence, ...]) -> SuggestionDraft:
    return SuggestionDraft(
        block_ref="b1",
        section_ref="s",
        change_type=kind,
        original_text="x",
        suggested_text="y",
        rationale=None,
        confidence_score=None,
        position=0,
        start_offset=0,
        end_offset=1,
        evidence=evidence,
    )


def _evidence(segment_index: int | None, match: EvidenceMatch = EvidenceMatch.EXACT) -> Evidence:
    return Evidence(
        source_position=0,
        source_name="Регламент",
        quote="q",
        match=match,
        segment_index=segment_index,
    )


def test_add_on_unchanged_only_for_add_with_all_evidence_unchanged() -> None:
    segment = SourceSegment(0, 0, 1, SegmentKind.PARAGRAPH)
    segments = {0: [(segment, 11), (segment, 12)]}
    drafts = [
        _draft(ChangeKind.ADD, (_evidence(0),)),
        _draft(ChangeKind.ADD, (_evidence(0), _evidence(1))),
        _draft(ChangeKind.MODIFY, (_evidence(0),)),
        _draft(ChangeKind.ADD, ()),
        _draft(ChangeKind.ADD, (_evidence(None, EvidenceMatch.NOT_FOUND),)),
    ]
    assert add_on_unchanged(drafts, segments, frozenset({11})) == [
        True,
        False,
        False,
        False,
        False,
    ]
