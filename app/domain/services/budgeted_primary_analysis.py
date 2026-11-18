"""Выбор полного плана первичного анализа по прогнозу суммарного входа."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.domain.interfaces.document_analyzer import AnalysisRequest, BlockPayload
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.services.claim_context import ClaimContextIndex
from app.domain.services.primary_analysis_request import PrimaryAnalysisPart
from app.domain.services.primary_context import PrimaryContextPolicy


@dataclass(frozen=True, slots=True)
class PlanBudget:
    max_input_tokens: int = 16_000
    group_limits: tuple[int, ...] = (8, 16, 32)

    def __post_init__(self) -> None:
        if self.max_input_tokens < 1 or not self.group_limits or min(self.group_limits) < 1:
            raise ValueError("Бюджет и размеры групп должны быть положительными")


@dataclass(frozen=True, slots=True)
class BudgetedPlan:
    parts: tuple[PrimaryAnalysisPart, ...]
    strategy: str
    estimated_input_tokens: int
    candidates: tuple[tuple[str, int, int], ...]


def build_budgeted_plan(
    request: AnalysisRequest,
    estimate: Callable[[AnalysisRequest], int],
    budget: PlanBudget = PlanBudget(),
    context_policy: PrimaryContextPolicy = PrimaryContextPolicy(max_units=256),
) -> BudgetedPlan:
    """Выбрать допустимый план без эталона и без потери редактируемых блоков.

    Если даже один блок не помещается в бюджет с выбранным контекстом,
    выбрасывается ValueError. Скрытого усечения текста нет.
    """
    index = ClaimContextIndex(request.sources, context_policy)
    refs = [block.ref for block in request.document.blocks]
    if len(refs) != len(set(refs)):
        raise ValueError("Идентификаторы блоков должны быть уникальными")
    request = replace(
        request,
        document=replace(
            request.document,
            blocks=tuple(
                replace(block, editable=False) if block.type is DocumentBlockType.HEADING else block
                for block in request.document.blocks
            ),
        ),
    )
    editable = tuple(
        block
        for block in request.document.blocks
        if block.editable and block.type is not DocumentBlockType.HEADING
    )
    if not editable:
        return BudgetedPlan((), "empty", 0, ())
    readonly = tuple(
        replace(block, editable=False)
        for block in request.document.blocks
        if not block.editable or block.type is DocumentBlockType.HEADING
    )
    selections = {block.ref: index.select(block.text) for block in editable}
    candidates: list[tuple[str, tuple[PrimaryAnalysisPart, ...], int]] = []
    full_cost = estimate(request)
    if full_cost <= budget.max_input_tokens:
        full = PrimaryAnalysisPart(request, index.full_context("whole_plan_direct"))
        candidates.append(("direct", (full,), full_cost))

    def part_for(group: tuple[BlockPayload, ...]) -> PrimaryAnalysisPart:
        context = index.combine(tuple(selections[block.ref] for block in group))
        readonly_refs = {block.ref for block in readonly}
        refs = {block.ref for block in group} | readonly_refs
        document = replace(
            request.document,
            blocks=tuple(
                replace(block, editable=False) if block.ref in readonly_refs else block
                for block in request.document.blocks
                if block.ref in refs
            ),
        )
        return PrimaryAnalysisPart(
            replace(
                request,
                request_id=f"{request.request_id}:v2:{group[0].ref}",
                document=document,
                sources=context.sources,
            ),
            context,
        )

    for limit in budget.group_limits:
        parts: list[PrimaryAnalysisPart] = []
        group: tuple[BlockPayload, ...] = ()
        valid = True
        for block in editable:
            proposed = (*group, block)
            if group and (
                len(proposed) > limit
                or estimate(part_for(proposed).request) > budget.max_input_tokens
            ):
                parts.append(part_for(group))
                proposed = (block,)
            if estimate(part_for(proposed).request) > budget.max_input_tokens:
                valid = False
                break
            group = proposed
        if valid:
            if group:
                parts.append(part_for(group))
            candidates.append(
                (
                    f"claims_max_{limit}",
                    tuple(parts),
                    sum(estimate(part.request) for part in parts),
                )
            )
    if not candidates:
        raise ValueError("Ни один план не помещается в заданный бюджет входа")
    strategy, chosen, cost = min(candidates, key=lambda item: (item[2], len(item[1])))
    return BudgetedPlan(
        chosen,
        strategy,
        cost,
        tuple((name, len(candidate), value) for name, candidate, value in candidates),
    )
