"""Контекст первичного анализа: отбор оснований, замыкание по зависимостям и план вызовов."""

import uuid
from dataclasses import replace

import pytest

from app.domain.interfaces.document_analyzer import AnalysisRequest, SourcePayload
from app.domain.interfaces.document_parser import DocumentBlockType, ParsedBlock
from app.domain.interfaces.source_connector import SourceKind
from app.domain.services.analysis_request_builder import DocumentInput, build_analysis_request
from app.domain.services.budgeted_primary_analysis import PlanBudget, build_budgeted_plan
from app.domain.services.claim_context import ClaimContextIndex, split_claims
from app.domain.services.primary_analysis_request import build_primary_analysis_requests
from app.domain.services.primary_context import PrimaryContextIndex, PrimaryContextPolicy

SPEC = "\n".join(
    [
        "# Таймауты",
        "3.1. Таймаут подключения шлюза равен 30 секунд.",
        "3.2. Повтор запроса выполняется через 5 секунд, как указано в пункте 3.1.",
        "# Журналы",
        "4.1. Журнал аудита хранится 90 дней.",
        "4.2. Архив журнала сжимается ежедневно.",
    ]
)


def _source(text: str = SPEC) -> SourcePayload:
    return SourcePayload("s1", "Спецификация", SourceKind.FILE, text)


def _request(
    texts: list[str], sources: list[SourcePayload], heading: bool = False
) -> AnalysisRequest:
    blocks, start = [], 0
    for position, text in enumerate(texts):
        kind = (
            DocumentBlockType.HEADING if heading and position == 0 else DocumentBlockType.PARAGRAPH
        )
        blocks.append(
            ParsedBlock(
                ref=f"b{position + 1}",
                type=kind,
                position=position,
                start_offset=start,
                end_offset=start + len(text),
                editable=True,
            )
        )
        start += len(text) + 1
    document = DocumentInput(uuid.UUID(int=1), "doc", "docx", "\n".join(texts), blocks)
    return build_analysis_request(
        job_id=uuid.UUID(int=2), attempt=0, document=document, sources=sources
    )


def test_policy_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        PrimaryContextPolicy(top_k=0)
    with pytest.raises(ValueError):
        PrimaryContextPolicy(max_share=0)


def test_reference_pulls_cited_clause_into_context() -> None:
    index = PrimaryContextIndex([_source()], PrimaryContextPolicy(top_k=1))
    selection = index.select("Повтор запроса выполняется через 5 секунд")
    assert selection.fallback is None
    text = selection.sources[0].text
    assert "3.2. Повтор" in text and "3.1. Таймаут" in text
    assert "Журнал аудита" not in text
    assert dict(selection.reasons)[0] == "dependency"


def test_unstructured_source_returns_full_context() -> None:
    plain = _source("Таймаут подключения равен 30 секунд.\nЖурнал хранится 90 дней.")
    selection = PrimaryContextIndex([plain], PrimaryContextPolicy()).select("Таймаут 30 секунд")
    assert selection.fallback == "unknown_structure"
    assert selection.sources == (plain,)


def test_unresolved_reference_returns_full_context() -> None:
    text = SPEC.replace("пункте 3.1", "пункте 7.7")
    selection = PrimaryContextIndex([_source(text)], PrimaryContextPolicy(top_k=1)).select(
        "Повтор запроса выполняется через 5 секунд"
    )
    assert selection.fallback == "unresolved_reference"
    assert selection.sources[0].text == text


def test_no_lexical_support_returns_full_context() -> None:
    selection = PrimaryContextIndex([_source()], PrimaryContextPolicy()).select("Цвет кнопки")
    assert selection.fallback == "no_lexical_support"


def test_claim_context_keeps_parent_condition() -> None:
    text = "\n".join(
        [
            "# Режимы",
            "Только для режима обслуживания действуют правила раздела.",
            "## Шлюз",
            "5.1. Таймаут шлюза равен 45 секунд.",
            "5.2. Порт шлюза 8443.",
            "## Журналы",
            "6.1. Журнал аудита хранится 90 дней.",
            "6.2. Архив журнала сжимается ежедневно.",
            "6.3. Копия архива передаётся в хранилище раз в неделю.",
        ]
    )
    index = ClaimContextIndex([_source(text)], PrimaryContextPolicy(top_k=1))
    selection = index.select("Таймаут шлюза 45 секунд.")
    assert selection.fallback is None
    rendered = selection.sources[0].text
    assert "Только для режима обслуживания" in rendered
    assert "Таймаут шлюза" in rendered
    assert "Журнал аудита" not in rendered


def test_split_claims_by_sentence() -> None:
    assert split_claims("Таймаут 30 секунд. Журнал 90 дней!  ") == (
        "Таймаут 30 секунд.",
        "Журнал 90 дней!",
    )


def test_primary_requests_keep_headings_and_source_dto() -> None:
    request = _request(
        ["Таймауты", "Таймаут подключения шлюза 20 секунд.", "Журнал аудита хранится 60 дней."],
        [_source()],
        heading=True,
    )
    parts = build_primary_analysis_requests(request, PrimaryContextPolicy(top_k=1))
    assert [part.request.request_id.rsplit(":", 1)[1] for part in parts] == ["0", "1"]
    for part, ref in zip(parts, ("b2", "b3"), strict=True):
        blocks = part.request.document.blocks
        assert [block.ref for block in blocks] == ["b1", ref]
        assert not blocks[0].editable
    assert "Журнал" not in parts[0].request.sources[0].text
    assert request.sources == (_source(),)


def _estimate(request: AnalysisRequest) -> int:
    return sum(len(block.text) for block in request.document.blocks) + sum(
        len(source.text) for source in request.sources
    )


def test_budgeted_plan_covers_each_editable_block_once() -> None:
    texts = ["Таймаут подключения шлюза 20 секунд.", "Журнал аудита хранится 60 дней."]
    request = _request(texts, [_source()])
    plan = build_budgeted_plan(request, _estimate, PlanBudget(group_limits=(1, 2)))
    editable = [
        block.ref for part in plan.parts for block in part.request.document.blocks if block.editable
    ]
    assert sorted(editable) == ["b1", "b2"]
    assert plan.estimated_input_tokens == min(cost for _, _, cost in plan.candidates)
    assert {name for name, _, _ in plan.candidates} >= {"direct", "claims_max_1", "claims_max_2"}


def test_budgeted_plan_makes_headings_readonly() -> None:
    request = _request(["Таймауты", "Таймаут подключения шлюза 20 секунд."], [_source()], True)
    plan = build_budgeted_plan(request, _estimate)
    for part in plan.parts:
        headings = [b for b in part.request.document.blocks if b.type is DocumentBlockType.HEADING]
        assert all(not block.editable for block in headings)


def test_budgeted_plan_without_editable_blocks_is_empty() -> None:
    request = _request(["Таймауты"], [_source()], heading=True)
    assert build_budgeted_plan(request, _estimate).strategy == "empty"


def test_budgeted_plan_rejects_budget_without_truncation() -> None:
    request = _request(["Таймаут подключения шлюза 20 секунд."], [_source()])
    with pytest.raises(ValueError):
        build_budgeted_plan(request, _estimate, PlanBudget(max_input_tokens=5))


def test_budgeted_plan_rejects_duplicate_refs() -> None:
    request = _request(["Первый.", "Второй."], [_source()])
    blocks = request.document.blocks
    duplicated = replace(
        request,
        document=replace(request.document, blocks=(blocks[0], replace(blocks[1], ref="b1"))),
    )
    with pytest.raises(ValueError):
        build_budgeted_plan(duplicated, _estimate)
