"""Изолированный, явно вызываемый план первичной поблочной актуализации."""

from dataclasses import dataclass, replace

from app.domain.interfaces.document_analyzer import AnalysisRequest
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.services.primary_context import (
    ContextSelection,
    PrimaryContextIndex,
    PrimaryContextPolicy,
)


@dataclass(frozen=True, slots=True)
class PrimaryAnalysisPart:
    request: AnalysisRequest
    context: ContextSelection


def build_primary_analysis_requests(
    request: AnalysisRequest,
    policy: PrimaryContextPolicy = PrimaryContextPolicy(),
    *,
    blocks_per_request: int = 1,
) -> tuple[PrimaryAnalysisPart, ...]:
    """Создать запрос для каждого проверяемого блока без изменения исходного DTO.

    Заголовки документа сохраняются как контекст. Воркер функцию пока не вызывает:
    режим выбирает вызывающий код явно.
    """
    if blocks_per_request < 1:
        raise ValueError("Размер группы должен быть положительным")
    index = PrimaryContextIndex(request.sources, policy)
    headings = tuple(
        replace(block, editable=False)
        for block in request.document.blocks
        if block.type is DocumentBlockType.HEADING
    )
    parts: list[PrimaryAnalysisPart] = []
    editable = tuple(
        block
        for block in request.document.blocks
        if block.editable and block.type is not DocumentBlockType.HEADING
    )
    for number in range(0, len(editable), blocks_per_request):
        group = editable[number : number + blocks_per_request]
        selections = tuple(index.select(block.text) for block in group)
        context = selections[0] if len(group) == 1 else index.combine(selections)
        document = replace(request.document, blocks=(*headings, *group))
        part = replace(
            request,
            request_id=f"{request.request_id}:primary:{number}",
            document=document,
            sources=context.sources,
        )
        parts.append(PrimaryAnalysisPart(part, context))
    return tuple(parts)
