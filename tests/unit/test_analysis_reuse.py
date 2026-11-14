"""Хэш входа анализа и решение о переносе результата."""

from __future__ import annotations

import uuid

import pytest

from app.domain.analysis_reuse import (
    AnalysisStrategy,
    Baseline,
    DeltaSummary,
    IncrementalMode,
    ReuseDecision,
    ReuseReason,
    RunInput,
    RunStats,
    analysis_input_hash,
    decide_reuse,
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
    )
    assert run_input.incremental_meta() == {
        "mode": "full",
        "reason": "input_changed",
        "baseline_job_id": str(BASELINE.job_id),
        "changed_share": 0.1235,
        "segments_added": 2,
        "segments_removed": 1,
    }
    assert run_input.run_stats_meta({"quick": 4})["harness"] == {"quick": 4}
    assert "harness" not in run_input.run_stats_meta({})
