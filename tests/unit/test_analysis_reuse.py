"""Хэш входа анализа и решение о переносе результата."""

from __future__ import annotations

import uuid

import pytest

from app.domain.analysis_reuse import (
    AnalysisStrategy,
    Baseline,
    DeltaFallback,
    DeltaPolicy,
    DeltaSummary,
    IncrementalMode,
    ReuseDecision,
    ReuseReason,
    RunInput,
    RunStats,
    analysis_input_hash,
    decide_reuse,
    delta_precheck,
    delta_scope_check,
    harness_counters,
)


def _hash(**overrides: object) -> str:
    params: dict[str, object] = {
        "contract_version": "2.0",
        "provider": "ml_http",
        "document_hash": "d" * 64,
        "sources": [("A", "a" * 64), ("B", "b" * 64)],
    }
    params.update(overrides)
    return analysis_input_hash(**params)  # type: ignore[arg-type]


def test_hash_ignores_source_order() -> None:
    assert _hash() == _hash(sources=[("B", "b" * 64), ("A", "a" * 64)])
    assert len(_hash()) == 64


@pytest.mark.parametrize(
    "override",
    [
        {"contract_version": "2.1"},
        {"provider": "ml_plugin"},
        {"document_hash": "e" * 64},
        {"sources": [("A", "a" * 64), ("B", "c" * 64)]},
        {"sources": [("A", "a" * 64), ("C", "b" * 64)]},
        {"sources": [("A", "a" * 64)]},
    ],
)
def test_hash_changes_with_any_input_part(override: dict[str, object]) -> None:
    assert _hash(**override) != _hash()


def test_names_with_separators_do_not_collide() -> None:
    assert _hash(sources=[("A,B", "x")]) != _hash(sources=[("A", "B,x")])


BASELINE = Baseline(job_id=uuid.uuid4(), input_hash="h" * 64)


@pytest.mark.parametrize(
    ("mode", "strategy", "baseline", "input_hash", "failed", "reason"),
    [
        (IncrementalMode.DELTA, AnalysisStrategy.AUTO, BASELINE, "h" * 64, 0, ReuseReason.REUSED),
        (IncrementalMode.REUSE, AnalysisStrategy.AUTO, BASELINE, "h" * 64, 0, ReuseReason.REUSED),
        (IncrementalMode.OFF, AnalysisStrategy.AUTO, BASELINE, "h" * 64, 0, ReuseReason.MODE_OFF),
        (
            IncrementalMode.DELTA,
            AnalysisStrategy.FULL,
            BASELINE,
            "h" * 64,
            0,
            ReuseReason.STRATEGY_FULL,
        ),
        (
            IncrementalMode.DELTA,
            AnalysisStrategy.AUTO,
            BASELINE,
            "h" * 64,
            1,
            ReuseReason.SOURCES_FAILED,
        ),
        (IncrementalMode.DELTA, AnalysisStrategy.AUTO, None, "h" * 64, 0, ReuseReason.NO_BASELINE),
        (
            IncrementalMode.DELTA,
            AnalysisStrategy.AUTO,
            BASELINE,
            "x" * 64,
            0,
            ReuseReason.INPUT_CHANGED,
        ),
    ],
)
def test_decide_reuse(
    mode: IncrementalMode,
    strategy: AnalysisStrategy,
    baseline: Baseline | None,
    input_hash: str,
    failed: int,
    reason: ReuseReason,
) -> None:
    decision = decide_reuse(
        mode=mode,
        strategy=strategy,
        input_hash=input_hash,
        baseline=baseline,
        failed_sources=failed,
    )
    assert decision.reason is reason
    assert decision.reuse is (reason is ReuseReason.REUSED)


def test_harness_counters_skip_unknown_and_non_numeric() -> None:
    meta = {"quick": 3, "full": 1, "conflict": True, "llm_seconds": 2.5, "add_checks": "x"}
    assert harness_counters(meta) == {"quick": 3, "full": 1, "llm_seconds": 2.5}
    assert harness_counters(None) == {}


def test_run_input_meta() -> None:
    run_input = RunInput(
        input_hash="h" * 64,
        contract_version="2.0",
        provider="ml_http",
        decision=ReuseDecision(ReuseReason.INPUT_CHANGED, BASELINE),
        stats=RunStats(10, 10, 100, 100, 5, 4),
        delta=DeltaSummary(changed_share=0.123456, segments_added=2, segments_removed=1),
        document_hash="d" * 64,
        fallback=DeltaFallback.CHANGED_SHARE,
    )
    assert run_input.incremental_meta() == {
        "mode": "full",
        "reason": "input_changed",
        "baseline_job_id": str(BASELINE.job_id),
        "chain_length": 0,
        "fallback_reason": "changed_share",
        "document_hash": "d" * 64,
        "changed_share": 0.1235,
        "segments_added": 2,
        "segments_removed": 1,
    }
    assert run_input.run_stats_meta({"quick": 4})["harness"] == {"quick": 4}
    assert "harness" not in run_input.run_stats_meta({})


FULL_BASELINE = Baseline(
    job_id=uuid.UUID(int=7),
    input_hash="b" * 64,
    document_hash="d" * 64,
    contract_version="2.0",
    provider="ml_http",
    chain_length=2,
)


def _precheck(baseline: Baseline = FULL_BASELINE, **overrides: object) -> DeltaFallback | None:
    arguments: dict[str, object] = {
        "mode": IncrementalMode.DELTA,
        "document_hash": "d" * 64,
        "contract_version": "2.0",
        "provider": "ml_http",
        "policy": DeltaPolicy(max_chain=5),
    }
    arguments.update(overrides)
    return delta_precheck(baseline, **arguments)  # type: ignore[arg-type]


def test_precheck_allows_selective_run_on_same_document() -> None:
    assert _precheck() is None


@pytest.mark.parametrize(
    ("baseline", "overrides", "fallback"),
    [
        (FULL_BASELINE, {"mode": IncrementalMode.REUSE}, DeltaFallback.MODE_REUSE),
        (BASELINE, {}, DeltaFallback.BASELINE_UNKNOWN),
        (FULL_BASELINE, {"document_hash": "e" * 64}, DeltaFallback.DOCUMENT_CHANGED),
        (FULL_BASELINE, {"contract_version": "2.1"}, DeltaFallback.CONTRACT_CHANGED),
        (FULL_BASELINE, {"provider": "stub"}, DeltaFallback.PROVIDER_CHANGED),
        (
            FULL_BASELINE,
            {"policy": DeltaPolicy(max_chain=2)},
            DeltaFallback.CHAIN_LIMIT,
        ),
    ],
)
def test_precheck_fallbacks(
    baseline: Baseline, overrides: dict[str, object], fallback: DeltaFallback
) -> None:
    assert _precheck(baseline, **overrides) is fallback


def test_scope_check_limits_changed_share_and_selected_blocks() -> None:
    policy = DeltaPolicy(max_changed_share=0.3, max_blocks_share=0.5)
    assert delta_scope_check(changed_share=0.3, selected=5, checkable=10, policy=policy) is None
    assert (
        delta_scope_check(changed_share=0.31, selected=1, checkable=10, policy=policy)
        is DeltaFallback.CHANGED_SHARE
    )
    assert (
        delta_scope_check(changed_share=0.1, selected=6, checkable=10, policy=policy)
        is DeltaFallback.BLOCKS_SHARE
    )


def test_chain_length_grows_only_on_selective_runs() -> None:
    def run(reason: ReuseReason, *, selective: bool = False) -> RunInput:
        return RunInput(
            input_hash="h" * 64,
            contract_version="2.0",
            provider="ml_http",
            decision=ReuseDecision(reason, FULL_BASELINE),
            stats=RunStats(1, 1, 1, 1, 1, 1),
            selective=selective,
        )

    selective = run(ReuseReason.INPUT_CHANGED, selective=True)
    assert (selective.mode, selective.chain_length) == ("delta", 3)
    assert selective.baseline_job_id == FULL_BASELINE.job_id
    reused = run(ReuseReason.REUSED)
    assert (reused.mode, reused.chain_length) == ("reused", 2)
    full = run(ReuseReason.INPUT_CHANGED)
    assert (full.mode, full.chain_length, full.baseline_job_id) == ("full", 0, None)
