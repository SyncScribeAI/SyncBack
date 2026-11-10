"""Домен анализа v2: сборка запроса, перевод результата в черновики, исход задачи.

Всё проверяется без БД и без инфраструктуры: правила, от которых зависит, применима ли
правка и каким будет статус задачи, не должны зависеть от устройства harness.
"""

import gc
import time
import tracemalloc
import uuid

import pytest

from app.domain.exceptions import LLMInputTooLargeError
from app.domain.interfaces.document_analyzer import (
    AnalysisError,
    AnalysisErrorCode,
    AnalysisResult,
    AnalysisStatus,
    BlockPayload,
    ChangeKind,
    FlagQuote,
    ProposedChange,
    ReviewFlag,
    SourcePayload,
)
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.interfaces.source_connector import SourceKind
from app.domain.lifecycle import AnalysisJobLifecycle
from app.domain.services.analysis_request_builder import (
    AnalysisLimits,
    DocumentInput,
    build_analysis_request,
)
from app.domain.services.analysis_result_mapper import NO_SECTION, map_result
from app.domain.value_objects import AnalysisJobStatusVO
from app.infrastructure.parsers.markdown_parser import MarkdownParser

JOB_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")

MD = (
    "Вступление без раздела.\n\n"
    "## Тарифы\n\n"
    "Срок ответа — 10 дней. Оплата картой.\n\n"
    "- Базовый тариф\n\n"
    "```\nprice = 10\n```\n"
)


def _document(text: str = MD) -> DocumentInput:
    parsed = MarkdownParser().parse(text.encode())
    return DocumentInput(
        id=DOC_ID, name="spec.md", format="md", plain_text=text, blocks=parsed.blocks
    )


def _source(name: str = "Решения") -> SourcePayload:
    return SourcePayload(
        id=str(uuid.uuid4()), name=name, type=SourceKind.FILE, text="Срок — 5 дней."
    )


def _request():
    return build_analysis_request(
        job_id=JOB_ID, attempt=2, document=_document(), sources=[_source()]
    )


# --- сборка запроса --------------------------------------------------------------


def test_request_carries_block_texts_and_ids() -> None:
    request = _request()
    assert request.request_id == f"{JOB_ID}:2"
    assert request.analysis_job_id == str(JOB_ID)
    assert request.document.id == str(DOC_ID)
    blocks = [(b.ref, b.type, b.text, b.editable) for b in request.document.blocks]
    assert blocks == [
        ("p-0", DocumentBlockType.PARAGRAPH, "Вступление без раздела.", True),
        ("h-1", DocumentBlockType.HEADING, "## Тарифы", False),
        ("p-2", DocumentBlockType.PARAGRAPH, "Срок ответа — 10 дней. Оплата картой.", True),
        ("li-3", DocumentBlockType.LIST_ITEM, "- Базовый тариф", True),
        ("c-4", DocumentBlockType.CODE, "```\nprice = 10\n```", False),
    ]
    assert [s.name for s in request.sources] == ["Решения"]
    parsed = MarkdownParser().parse(MD.encode()).blocks
    assert [b.start_offset for b in request.document.blocks] == [b.start_offset for b in parsed]


def test_duplicate_block_ref_is_our_error_not_harness_one() -> None:
    document = _document()
    blocks = list(document.blocks)
    duplicated = DocumentInput(
        id=DOC_ID, name="x", format="md", plain_text=MD, blocks=[blocks[0], blocks[0]]
    )
    with pytest.raises(ValueError, match="p-0"):
        build_analysis_request(job_id=JOB_ID, attempt=0, document=duplicated, sources=[])


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (AnalysisLimits(max_blocks=4), "больше 4 блоков"),
        (AnalysisLimits(max_block_chars=20), "Блок p-0"),
        (AnalysisLimits(max_sources=0), "Источников 1"),
    ],
)
def test_limits_reject_before_calling_harness(limits: AnalysisLimits, message: str) -> None:
    with pytest.raises(LLMInputTooLargeError, match=message):
        build_analysis_request(
            job_id=JOB_ID, attempt=0, document=_document(), sources=[_source()], limits=limits
        )


def test_block_payload_requires_ref_and_text() -> None:
    with pytest.raises(ValueError):
        BlockPayload(ref="", type=DocumentBlockType.PARAGRAPH, text="a", editable=True)
    with pytest.raises(ValueError):
        BlockPayload(ref="p-0", type=DocumentBlockType.PARAGRAPH, text="", editable=True)


# --- перевод результата ------------------------------------------------------------


def _completed(*changes: ProposedChange, flags: tuple[ReviewFlag, ...] = ()) -> AnalysisResult:
    return AnalysisResult(status=AnalysisStatus.COMPLETED, suggestions=changes, flags=flags)


def test_suggestions_get_section_position_and_anchor() -> None:
    result = _completed(
        ProposedChange(
            "modify", "p-0", "без раздела", "вне разделов", rationale="r", confidence=0.95
        ),
        ProposedChange("modify", "p-2", "10 дней", "5 дней", confidence=0.9504),
        ProposedChange("add", "p-2", None, "Оплата по счёту.", anchor_text="Оплата картой."),
    )
    mapped = map_result(result, _request().document.blocks)
    drafts = [
        (d.block_ref, d.section_ref, d.change_type, d.original_text, d.suggested_text, d.position)
        for d in mapped.suggestions
    ]
    assert drafts == [
        ("p-0", NO_SECTION, ChangeKind.MODIFY, "без раздела", "вне разделов", 0),
        ("p-2", "Тарифы", ChangeKind.MODIFY, "10 дней", "5 дней", 1),
        ("p-2", "Тарифы", ChangeKind.ADD, "Оплата картой.", "Оплата по счёту.", 2),
    ]
    assert [d.confidence_score for d in mapped.suggestions] == [0.95, 0.95, None]
    assert mapped.dropped == {}


def test_offsets_point_into_document_text_like_editor_blocks() -> None:
    # «10» есть и в p-2, и в коде c-4: смещение ищется в своём блоке, а не по документу.
    text = MD + "\nСрок 10 дней, ещё раз 10 дней.\n"
    request = build_analysis_request(
        job_id=JOB_ID, attempt=0, document=_document(text), sources=[_source()]
    )
    last = request.document.blocks[-1].ref
    result = _completed(
        ProposedChange("modify", "p-2", "10 дней", "5 дней"),
        ProposedChange("add", "p-2", None, "Оплата по счёту.", anchor_text="Оплата картой."),
        ProposedChange("delete", last, "10 дней", None),
    )
    mapped = map_result(result, request.document.blocks)
    for draft in mapped.suggestions:
        assert text[draft.start_offset : draft.end_offset] == draft.original_text
    # Одна правка на повторяющуюся фразу — первое вхождение.
    last_block = request.document.blocks[-1]
    assert mapped.suggestions[2].start_offset == last_block.start_offset + len("Срок ")


def test_repeated_phrase_in_block_gets_occurrences_in_answer_order() -> None:
    text = MD + "\nСрок 10 дней, ещё раз 10 дней.\n"
    request = build_analysis_request(
        job_id=JOB_ID, attempt=0, document=_document(text), sources=[_source()]
    )
    block = request.document.blocks[-1]
    result = _completed(
        ProposedChange("modify", block.ref, "10 дней", "5 дней"),
        ProposedChange("modify", block.ref, "10 дней", "7 дней"),
        # Третьего вхождения нет: правка легла бы на уже занятое место.
        ProposedChange("delete", block.ref, "10 дней", None),
        ProposedChange("add", block.ref, None, "Первое.", anchor_text="Срок"),
        ProposedChange("add", block.ref, None, "Второе.", anchor_text="Срок"),
    )
    mapped = map_result(result, request.document.blocks)
    starts = [d.start_offset - block.start_offset for d in mapped.suggestions]
    assert starts == [len("Срок "), len("Срок 10 дней, ещё раз "), 0, 0]
    assert [d.suggested_text for d in mapped.suggestions[:2]] == ["5 дней", "7 дней"]
    assert mapped.dropped == {"old_text_occurrences_exhausted": 1}
    for draft in mapped.suggestions:
        assert text[draft.start_offset : draft.end_offset] == draft.original_text


def test_section_ref_matches_editor_sections() -> None:
    sections = {s.ref for s in MarkdownParser().parse(MD.encode()).sections}
    mapped = map_result(
        _completed(ProposedChange("modify", "p-2", "10", "5")), _request().document.blocks
    )
    assert mapped.suggestions[0].section_ref in sections


def test_inapplicable_changes_are_dropped_by_reason_and_positions_stay_dense() -> None:
    result = _completed(
        ProposedChange("modify", "p-404", "x", "y"),
        ProposedChange("modify", "h-1", "Тарифы", "Цены"),
        ProposedChange("modify", "p-2", "20 дней", "5 дней"),
        ProposedChange("modify", "p-2", "  ", "5 дней"),
        ProposedChange("add", "p-2", None, "Новое.", anchor_text=None),
        ProposedChange("add", "p-2", None, "Новое.", anchor_text="Оплата наличными."),
        ProposedChange("add", "p-2", None, " ", anchor_text="Оплата картой."),
        ProposedChange("rewrite", "p-2", "10", "5"),
        ProposedChange("modify", "p-2", "10 дней", "5 дней"),
    )
    mapped = map_result(result, _request().document.blocks)
    assert mapped.dropped == {
        "block_not_found": 1,
        "block_not_editable": 1,
        "old_text_not_in_block": 1,
        "old_text_missing": 1,
        "add_without_anchor": 1,
        "anchor_not_in_block": 1,
        "add_without_text": 1,
        "unknown_change_type": 1,
    }
    assert [(d.original_text, d.position) for d in mapped.suggestions] == [("10 дней", 0)]


def test_modify_without_new_text_becomes_delete() -> None:
    mapped = map_result(
        _completed(
            ProposedChange("modify", "p-2", "Оплата картой.", ""),
            ProposedChange("delete", "li-3", "Базовый", "лишнее"),
        ),
        _request().document.blocks,
    )
    assert [(d.change_type, d.suggested_text) for d in mapped.suggestions] == [
        (ChangeKind.DELETE, None),
        (ChangeKind.DELETE, None),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1.7, 1.0), (-0.2, 0.0), (0.12345, 0.123), (float("nan"), None), (True, None), (None, None)],
)
def test_confidence_is_clamped_for_numeric_column(
    raw: float | None, expected: float | None
) -> None:
    mapped = map_result(
        _completed(ProposedChange("modify", "p-2", "10", "5", confidence=raw)),
        _request().document.blocks,
    )
    assert mapped.suggestions[0].confidence_score == expected


def test_flags_keep_unknown_reason_and_use_our_block_text() -> None:
    quotes = (FlagQuote("A", "5 дней"), FlagQuote("B", "7 дней"))
    mapped = map_result(
        _completed(
            flags=(
                ReviewFlag("sources_conflict", "p-2", "искажённая копия", "спор", quotes),
                ReviewFlag("new_reason", "h-1", "## Тарифы"),
                ReviewFlag("", "p-404", "x"),
            )
        ),
        _request().document.blocks,
    )
    assert [(f.block_ref, f.reason, f.section_ref, f.position) for f in mapped.flags] == [
        ("p-2", "sources_conflict", "Тарифы", 0),
        ("h-1", "new_reason", "Тарифы", 1),
    ]
    assert mapped.flags[0].original_text == "Срок ответа — 10 дней. Оплата картой."
    assert mapped.flags[0].quotes == quotes
    assert mapped.dropped == {"flag_block_not_found": 1}


# --- исход задачи --------------------------------------------------------------------


def _failed(code: AnalysisErrorCode, message: str = "сбой") -> AnalysisResult:
    return AnalysisResult(status=AnalysisStatus.FAILED, error=AnalysisError(code, message))


def test_invalid_request_is_final_and_pipeline_error_is_retryable() -> None:
    final = AnalysisJobLifecycle.outcome_for(_failed(AnalysisErrorCode.INVALID_REQUEST))
    assert (final.status, final.error_code, final.retryable) == (
        AnalysisJobStatusVO.FAILED,
        "ML_INVALID_REQUEST",
        False,
    )
    retry = AnalysisJobLifecycle.outcome_for(_failed(AnalysisErrorCode.PIPELINE_ERROR, "x" * 900))
    assert (retry.error_code, retry.retryable) == ("LLM_UNAVAILABLE", True)
    assert retry.error_message is not None and len(retry.error_message) == 500


def test_completed_is_success_only_when_everything_was_checked() -> None:
    assert AnalysisJobLifecycle.outcome_for(_completed()).status is AnalysisJobStatusVO.SUCCESS
    partial = AnalysisJobLifecycle.outcome_for(
        AnalysisResult(status=AnalysisStatus.COMPLETED, meta={"errors": 3}), ["Wiki"]
    )
    assert (partial.status, partial.error_code, partial.error_message) == (
        AnalysisJobStatusVO.PARTIAL_SUCCESS,
        "PARTIAL_FAILURE",
        "3 блоков не удалось проверить; недоступны источники: Wiki",
    )


@pytest.mark.parametrize("errors", [None, "3", True, -1, 0, float("nan")])
def test_meta_errors_is_read_softly(errors: object) -> None:
    result = AnalysisResult(status=AnalysisStatus.COMPLETED, meta={"errors": errors})
    assert AnalysisJobLifecycle.outcome_for(result).status is AnalysisJobStatusVO.SUCCESS


# --- бюджеты сложности и памяти ---------------------------------------------------------


def _big_case(blocks: int, changes: int) -> tuple[DocumentInput, AnalysisResult]:
    paragraph = "Срок ответа — 10 рабочих дней, оплата картой. " * 6  # ~300 символов
    text = "\n\n".join(f"## Раздел {i}" if i % 20 == 0 else paragraph for i in range(blocks))
    document = _document(text)
    refs = [b.ref for b in document.blocks if b.ref.startswith("p-")]
    step = max(len(refs) // max(changes, 1), 1)
    result = _completed(
        *(
            ProposedChange("modify", refs[i * step], "10 рабочих", "5 рабочих")
            for i in range(changes)
        )
    )
    return document, result


def _timed_build_and_map(document: DocumentInput, result: AnalysisResult, expected: int) -> float:
    # В полном прогоне под coverage куча велика, и проходы GC по ней зависят от числа
    # тестов, а не от входа замера; минимум из нескольких прогонов отсекает шум раннера.
    gc.collect()
    gc.disable()
    try:
        started = time.perf_counter()
        request = build_analysis_request(job_id=JOB_ID, attempt=0, document=document, sources=[])
        mapped = map_result(result, request.document.blocks)
        elapsed = time.perf_counter() - started
    finally:
        gc.enable()
    assert len(mapped.suggestions) == expected
    return elapsed


@pytest.mark.perf
def test_builder_and_mapper_scale_linearly_with_bounded_memory() -> None:
    timings = []
    for scale in (1, 4):
        document, result = _big_case(2000 * scale, 500 * scale)
        timings.append(min(_timed_build_and_map(document, result, 500 * scale) for _ in range(3)))
    # Квадратичный код дал бы ×16; запас на шум CI.
    assert timings[1] < timings[0] * 8

    document, _ = _big_case(2000, 0)
    tracemalloc.start()
    build_analysis_request(job_id=JOB_ID, attempt=0, document=document, sources=[])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    # Тексты блоков в UTF-8-строках Python занимают ~2 байта на кириллический символ.
    assert peak <= 3 * 2 * len(document.plain_text)
