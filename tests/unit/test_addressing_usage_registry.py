"""Строгая адресация правок, учёт токенов, ячейки таблиц DOCX и реестр зависимостей."""

import io
import uuid
from typing import Any

import pytest
from docx import Document as DocxDocument

from app.domain.analysis_reuse import (
    Baseline,
    DeltaFallback,
    DeltaPolicy,
    analysis_input_hash,
)
from app.domain.interfaces.document_analyzer import (
    AnalysisResult,
    AnalysisStatus,
    ProposedChange,
    SourcePayload,
)
from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.interfaces.document_parser import DocumentBlockType, ParsedBlock
from app.domain.interfaces.source_connector import SourceKind
from app.domain.llm_usage import LlmCall, LlmUsage, usage_from_records
from app.domain.pipeline_run import summarize_run
from app.domain.services.analysis_request_builder import DocumentInput, build_analysis_request
from app.domain.services.analysis_result_mapper import map_result
from app.domain.services.delta_request import (
    BaselineFingerprints,
    DeltaRequest,
    build_delta_request,
)
from app.domain.services.delta_selection import BlockText, DeltaLimits
from app.domain.services.dependency_registry import (
    build_registry,
    document_context_refs,
    select_by_registry,
    source_segments,
)
from app.domain.services.edit_locator import LocateRefusal, TextAnchor, locate
from app.domain.source_snapshot import segment_fingerprints
from app.infrastructure.exporters.docx_exporter import DocxExporter
from app.infrastructure.ml_pipeline.schemas import decode_response, usage_from_meta
from app.infrastructure.parsers.docx_parser import DocxParser

# --- локатор ----------------------------------------------------------------------

TEXT = "Срок 10 дней, ещё раз 10 дней."


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        (TextAnchor("10 дней"), LocateRefusal.AMBIGUOUS),
        (TextAnchor("10 дней", occurrence=2), 22),
        (TextAnchor("10 дней", prefix="Срок "), 5),
        (TextAnchor("10 дней", suffix="."), 22),
        (TextAnchor("10 дней", prefix="раз ", occurrence=1), LocateRefusal.CONTEXT_MISMATCH),
        (TextAnchor("10 дней", occurrence=3), LocateRefusal.INVALID_OCCURRENCE),
        (TextAnchor("10 дней", occurrence=0), LocateRefusal.INVALID_OCCURRENCE),
        (TextAnchor("20 дней"), LocateRefusal.NOT_IN_BLOCK),
        (TextAnchor("  "), LocateRefusal.TEXT_MISSING),
        # Контекст за границей блока не принимается: блок начинается с «Срок».
        (TextAnchor("Срок", prefix="\n"), LocateRefusal.CONTEXT_MISMATCH),
    ],
)
def test_locate_accepts_exactly_one_candidate(anchor: TextAnchor, expected: object) -> None:
    result = locate(TEXT, anchor)
    if isinstance(expected, int):
        assert not isinstance(result, LocateRefusal)
        assert result.start == expected
    else:
        assert result == expected


def test_locate_refuses_taken_place() -> None:
    assert locate(TEXT, TextAnchor("10 дней", occurrence=1), {5}) == LocateRefusal.TAKEN


# --- схема ответа ML и учёт usage --------------------------------------------------


def _ml_response(**change: Any) -> dict[str, Any]:
    return {
        "contract_version": "2.0",
        "request_id": "r-1",
        "status": "completed",
        "suggestions": [
            {
                "suggestion_id": "s-1",
                "change_type": "modify",
                "target_block_id": "p-1",
                "old_text": "10",
                "new_text": "5",
                **change,
            }
        ],
        "meta": {
            "llm_calls": [
                {"stage": "quick", "input_tokens": 100, "output_tokens": 20},
                {"stage": "full", "input_tokens": 300, "output_tokens": None},
            ]
        },
    }


def test_schema_reads_address_fields_and_usage() -> None:
    result = decode_response(
        _ml_response(context_before="Срок ", context_after=" дней", occurrence=2),
        expected_request_id="r-1",
    )
    change = result.suggestions[0]
    assert (change.context_before, change.context_after, change.occurrence) == (
        "Срок ",
        " дней",
        2,
    )
    assert result.usage is not None
    assert result.usage.input_tokens == 400
    # Неизвестный счётчик выхода делает итог неполным, а не нулевым.
    assert result.usage.complete is False


@pytest.mark.parametrize("value", ["2", -1, 0, True, 1.5])
def test_schema_drops_invalid_occurrence(value: object) -> None:
    result = decode_response(_ml_response(occurrence=value), expected_request_id="r-1")
    assert result.suggestions[0].occurrence is None


def test_usage_absent_is_none_not_zero() -> None:
    assert usage_from_meta({}) is None
    assert (
        usage_from_records([{"stage": "x", "input_tokens": "много"}]).calls[0].input_tokens is None
    )


def test_usage_reasoning_is_not_added_twice() -> None:
    usage = LlmUsage(
        (LlmCall("direct", "m", input_tokens=1000, output_tokens=300, reasoning_tokens=200),)
    )
    assert usage.total_tokens == 1300
    meta = usage.as_meta()
    assert meta["reasoning_tokens"] == 200
    assert meta["by_stage"] == {"direct": {"calls": 1, "input_tokens": 1000, "output_tokens": 300}}


def test_run_summary_keeps_usage_outside_meta_limit() -> None:
    result = AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        meta={"blob": "x" * 20_000},
        usage=LlmUsage((LlmCall("direct", "m", 10, 2),)),
    )
    run = summarize_run(result, request_id="r", contract_version="2.0", provider="ml_http")
    assert run.meta is None
    assert run.usage is not None and run.usage["input_tokens"] == 10


def test_pipeline_version_changes_hash_only_when_set() -> None:
    base = dict(contract_version="2.0", provider="ml_http", document_hash="d", sources=[])
    assert analysis_input_hash(**base) == analysis_input_hash(**base, pipeline_version="")
    assert analysis_input_hash(**base) != analysis_input_hash(**base, pipeline_version="p2")


# --- DOCX: ячейки таблиц и экспорт без поиска по документу --------------------------


def _docx_with_table() -> bytes:
    document = DocxDocument()
    document.add_paragraph("Ответ 10 дней.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Узел A"
    table.cell(0, 1).text = "10 дней"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraph_texts(raw: bytes) -> list[str]:
    document = DocxDocument(io.BytesIO(raw))
    cells = [cell.text for row in document.tables[0].rows for cell in row.cells]
    return [p.text for p in document.paragraphs if p.text] + cells


def test_table_cells_are_editable_only_by_setting() -> None:
    raw = _docx_with_table()
    default = DocxParser().parse(raw)
    enabled = DocxParser(table_cells_editable=True).parse(raw)
    table = [b for b in default.blocks if b.type is DocumentBlockType.TABLE]
    assert table and not any(b.editable for b in table)
    assert all(b.editable for b in enabled.blocks if b.type is DocumentBlockType.TABLE)


def test_table_cell_change_is_exported_at_its_address() -> None:
    raw = _docx_with_table()
    parsed = DocxParser(table_cells_editable=True).parse(raw)
    cell = next(
        b for b in parsed.blocks if parsed.plain_text[b.start_offset : b.end_offset] == "10 дней"
    )
    change = AppliedChange(
        section_ref="",
        change_type="modify",
        old_text="10 дней",
        new_text="5 дней",
        start_offset=cell.start_offset,
        end_offset=cell.end_offset,
        block_ref=cell.ref,
    )
    out, report = DocxExporter().apply_changes_with_report(raw, [change])
    assert report.unapplied == 0
    assert _paragraph_texts(out) == ["Ответ 10 дней.", "Узел A", "5 дней"]


def test_addressed_change_is_not_moved_to_first_match() -> None:
    raw = _docx_with_table()
    # Адрес указывает мимо текста: прежний поиск по документу заменил бы абзац тела.
    stale = AppliedChange(
        section_ref="",
        change_type="modify",
        old_text="10 дней",
        new_text="5 дней",
        start_offset=0,
        end_offset=7,
        block_ref="t-9",
    )
    out, report = DocxExporter().apply_changes_with_report(raw, [stale])
    assert report.unapplied == 1
    assert report.reasons == ["адрес правки не совпал с текстом блока"]
    assert _paragraph_texts(out) == ["Ответ 10 дней.", "Узел A", "10 дней"]


# --- реестр зависимостей и выборочный запрос ----------------------------------------

LIMITS = DeltaLimits(support_segments=2)
OLD = (
    "# Узлы\n\nУзел A: тайм-аут 310 секунд.\n\nУзел B: тайм-аут 120 секунд.\n\n"
    "# Журнал\n\nЖурнал хранится 30 дней.\n\nЖурнал хранится 30 дней.\n"
)
BLOCKS = [
    BlockText("b1", "Для узла A тайм-аут равен 310 секунд."),
    BlockText("b2", "Для узла B тайм-аут равен 120 секунд."),
    BlockText("b3", "Записи журнала хранятся 30 дней."),
    BlockText("b4", "Оформление титульного листа произвольное."),
]


def _select(new: str) -> Any:
    return select_by_registry(
        BLOCKS,
        [source_segments(OLD)],
        [source_segments(new)],
        evidence_changed=(),
        flagged=(),
        limits=LIMITS,
    )


def test_registry_links_blocks_to_baseline_segments() -> None:
    registry = build_registry(BLOCKS, [source_segments(OLD)], LIMITS)
    assert registry.segments["b1"] and registry.segments["b3"]
    old = {s.text: s.fingerprint for s in source_segments(OLD)}
    assert old["Узел A: тайм-аут 310 секунд."] in registry.segments["b1"]


def test_deleted_segment_selects_its_dependents() -> None:
    selected = _select(OLD.replace("Узел A: тайм-аут 310 секунд.\n\n", ""))
    assert "b1" in selected.refs
    assert selected.by_rule["r1_removed_dependency"] >= 1
    assert selected.unknown_scope is False


def test_removing_one_copy_of_duplicate_segment_is_a_change() -> None:
    selected = _select(OLD.replace("Журнал хранится 30 дней.\n\nЖурнал", "Журнал", 1))
    assert "b3" in selected.refs


def test_block_without_dependencies_is_always_selected() -> None:
    selected = _select(OLD.replace("310", "327"))
    assert "b4" in selected.refs
    assert {"b1", "b4"} <= selected.refs


def test_unrelated_new_segment_widens_to_all_blocks() -> None:
    selected = _select("# Прочее\n\nНовое требование к отчётности.\n\n" + OLD)
    assert selected.unknown_scope is True
    assert selected.refs == {b.ref for b in BLOCKS}


def test_document_context_keeps_headings_and_neighbours() -> None:
    blocks = [("h", True), ("a", False), ("b", False), ("c", False), ("d", False)]
    assert document_context_refs(blocks, {"c"}, 0) == {"h", "c"}
    assert document_context_refs(blocks, {"c"}, 1) == {"h", "b", "c", "d"}


def _document(texts: list[str]) -> DocumentInput:
    blocks, start = [], 0
    for position, text in enumerate(texts):
        blocks.append(
            ParsedBlock(
                ref=f"b{position + 1}",
                type=DocumentBlockType.PARAGRAPH,
                position=position,
                start_offset=start,
                end_offset=start + len(text),
                editable=True,
            )
        )
        start += len(text) + 1
    return DocumentInput(uuid.UUID(int=1), "doc", "docx", "\n".join(texts), blocks)


def _delta(new: str, **options: Any) -> DeltaRequest | DeltaFallback:
    old = segment_fingerprints(OLD)
    return build_delta_request(
        baseline=Baseline(uuid.UUID(int=3), "h"),
        fingerprints=BaselineFingerprints(
            all=frozenset(fp for _, fp in old), by_snapshot={"s": [fp for _, fp in old]}
        ),
        document=_document([block.text for block in BLOCKS]),
        payloads=[SourcePayload("s1", "Спецификация", SourceKind.FILE, new)],
        segments=[segment_fingerprints(new)],
        suggestions=[],
        flags=[],
        policy=DeltaPolicy(max_changed_share=1.0, max_blocks_share=0.9),
        limits=LIMITS,
        **options,
    )


def test_lexical_delta_misses_block_of_deleted_segment() -> None:
    # Исходное поведение: удалённый текст недоступен отбору по словам.
    planned = _delta(OLD.replace("Узел A: тайм-аут 310 секунд.\n\n", ""))
    assert isinstance(planned, DeltaRequest)
    assert "b1" not in planned.selected.refs


def test_registry_delta_selects_and_reduces_document() -> None:
    planned = _delta(
        OLD.replace("Узел A: тайм-аут 310 секунд.\n\n", ""),
        baseline_texts=[OLD],
        strategy="registry",
        context_blocks=0,
    )
    assert isinstance(planned, DeltaRequest)
    assert "b1" in planned.selected.refs
    assert planned.document_refs == planned.selected.refs
    assert planned.selected.as_meta()["selected_by_r1_removed_dependency"] >= 1


def test_wide_registry_selection_follows_setting() -> None:
    new = "# Прочее\n\nНовое требование к отчётности.\n\n" + OLD
    options: dict[str, Any] = {"baseline_texts": [OLD], "strategy": "registry"}
    assert _delta(new, **options) is DeltaFallback.BLOCKS_SHARE
    planned = _delta(new, **options, wide_selection="all_blocks")
    assert isinstance(planned, DeltaRequest)
    assert planned.selected.refs == {b.ref for b in BLOCKS}


def test_registry_requires_baseline_texts() -> None:
    with pytest.raises(ValueError, match="прошлых снимков"):
        _delta(OLD, strategy="registry")


# --- построчное разбиение источников ---------------------------------------------

NUMBERED_OLD = (
    "# Спецификация узла A\n"
    "300.1. Для узла A тайм-аут 310 секунд.\n"
    "300.2. Узел A пишет журнал 30 дней.\n"
    "300.3. Узел A хранит архив 12 месяцев.\n"
    "300.4. Узел A принимает 40 соединений.\n"
    "300.5. Узел A проверяет подпись поставщика.\n"
)


def test_line_segments_split_numbered_items_without_blank_lines() -> None:
    paragraph = [s.text for s in source_segments(NUMBERED_OLD)]
    line = [s.text for s in source_segments(NUMBERED_OLD, by_line=True)]
    assert len(paragraph) == 2
    assert line[1] == "300.1. Для узла A тайм-аут 310 секунд."
    assert len(line) == 6


def test_line_segments_keep_indented_continuation_of_item() -> None:
    text = "1.2. Пункт\n  продолжение пункта\nОбычная строка"
    assert [s.text for s in source_segments(text, by_line=True)] == [
        "1.2. Пункт\n  продолжение пункта",
        "Обычная строка",
    ]


def test_stored_fingerprints_keep_paragraph_segmentation() -> None:
    # По этим отпечаткам идёт сравнение со старыми снимками, поэтому разбиение прежнее.
    assert len(segment_fingerprints(NUMBERED_OLD)) == 2


def _numbered_delta(new: str, **options: Any) -> DeltaRequest | DeltaFallback:
    old = segment_fingerprints(NUMBERED_OLD)
    return build_delta_request(
        baseline=Baseline(uuid.UUID(int=3), "h"),
        fingerprints=BaselineFingerprints(
            all=frozenset(fp for _, fp in old), by_snapshot={"s": [fp for _, fp in old]}
        ),
        document=_document(["Для узла A тайм-аут равен 310 секунд.", "Архив хранится год."]),
        payloads=[SourcePayload("s1", "Спецификация", SourceKind.FILE, new)],
        segments=[segment_fingerprints(new)],
        suggestions=[],
        flags=[],
        policy=DeltaPolicy(max_changed_share=1.0, max_blocks_share=0.9),
        limits=DeltaLimits(context_segments=0, support_segments=0),
        baseline_texts=[NUMBERED_OLD],
        strategy="registry",
        wide_selection="all_blocks",
        **options,
    )


def test_line_granularity_sends_only_changed_item() -> None:
    new = NUMBERED_OLD.replace("310", "327")
    paragraph = _numbered_delta(new)
    line = _numbered_delta(new, source_granularity="line")
    assert isinstance(paragraph, DeltaRequest) and isinstance(line, DeltaRequest)
    assert "b1" in line.selected.refs
    assert line.sources[0].text == "# Спецификация узла A\n\n300.1. Для узла A тайм-аут 327 секунд."
    assert line.stats.chars_sent < paragraph.stats.chars_sent
    # Основание прошлых правок по-прежнему сверяется с абзацными отпечатками.
    assert line.base == paragraph.base


def test_strong_support_sends_value_line_of_unchanged_block() -> None:
    old = (
        "# Узел N1K11\n"
        "1.1. Для узла N1K11 тайм-аут 310 секунд.\n"
        "1.2. Перед активацией узла дежурный сверяет подпись и журнал.\n"
        "# Узел N1K22\n"
        "2.1. Для узла N1K22 тайм-аут 120 секунд.\n"
        "2.2. Перед активацией узла дежурный сверяет подпись и журнал.\n"
    )
    new = old.replace("310", "327")
    blocks = [
        BlockText("b1", "Узел N1K11: тайм-аут 310 секунд."),
        BlockText(
            "b2", "Перед активацией узла N1K22 тайм-аут 120 секунд, дежурный сверяет подпись."
        ),
    ]
    from app.domain.services.delta_selection import SourceInput, reduce_sources

    base = frozenset(fp for _, fp in segment_fingerprints(old, by_line=True))
    source = SourceInput(new, segment_fingerprints(new, by_line=True))
    weak = reduce_sources(
        [source], base, [blocks[1].text], DeltaLimits(context_segments=0, support_segments=0)
    )
    strong = reduce_sources(
        [source],
        base,
        [blocks[1].text],
        DeltaLimits(context_segments=0, support_segments=0, strong_support_tokens=2),
    )
    value_line = "2.1. Для узла N1K22 тайм-аут 120 секунд."
    assert value_line not in weak[0].text
    assert value_line in strong[0].text
    # Строка с одним общим токеном (узел без значения) опорой не считается.
    assert "2.2." not in strong[0].text


def test_line_granularity_requires_baseline_texts() -> None:
    old = segment_fingerprints(NUMBERED_OLD)
    with pytest.raises(ValueError, match="прошлых снимков"):
        build_delta_request(
            baseline=Baseline(uuid.UUID(int=3), "h"),
            fingerprints=BaselineFingerprints(all=frozenset(fp for _, fp in old), by_snapshot={}),
            document=_document(["Блок"]),
            payloads=[SourcePayload("s1", "S", SourceKind.FILE, NUMBERED_OLD)],
            segments=[old],
            suggestions=[],
            flags=[],
            policy=DeltaPolicy(),
            limits=LIMITS,
            source_granularity="line",
        )


# --- прямой анализ ---------------------------------------------------------------


def test_proposed_change_defaults_keep_old_contract() -> None:
    change = ProposedChange("modify", "p-1", "a", "b")
    assert (change.context_before, change.context_after, change.occurrence) == (None, None, None)
