"""Сводка вызова ML: счётчики из meta и защита строки задачи от большой meta."""

from typing import Any

import pytest

from app.domain.interfaces.document_analyzer import AnalysisResult, AnalysisStatus
from app.domain.pipeline_run import META_MAX_BYTES, failed_block_count, summarize_run


def _summary(meta: dict[str, Any]):
    return summarize_run(
        AnalysisResult(status=AnalysisStatus.COMPLETED, meta=meta),
        request_id="job:1",
        contract_version="2.0",
        provider="ml_http",
    )


def test_counters_are_taken_from_meta() -> None:
    run = _summary({"checked_blocks": 12, "errors": 2, "llm_seconds": 3.5})
    assert (run.request_id, run.contract_version, run.provider) == ("job:1", "2.0", "ml_http")
    assert (run.checked_blocks, run.failed_blocks) == (12, 2)
    assert run.meta == {"checked_blocks": 12, "errors": 2, "llm_seconds": 3.5}
    assert not run.meta_dropped


@pytest.mark.parametrize("value", [None, "12", True, float("nan"), float("inf"), [1]])
def test_non_numeric_counter_is_unknown(value: object) -> None:
    run = _summary({"checked_blocks": value, "errors": value})
    assert (run.checked_blocks, run.failed_blocks) == (None, None)
    assert failed_block_count({"errors": value}) == 0


def test_negative_counter_is_clamped() -> None:
    assert _summary({"errors": -3}).failed_blocks == 0


def test_oversized_meta_is_dropped_but_counters_kept() -> None:
    run = _summary({"checked_blocks": 5, "trace": "я" * META_MAX_BYTES})
    assert run.meta_dropped
    assert run.checked_blocks == 5


def test_meta_with_nan_is_dropped_because_jsonb_rejects_it() -> None:
    run = _summary({"llm_seconds": float("nan"), "checked_blocks": 1})
    assert run.meta_dropped
    assert run.checked_blocks == 1


def test_non_json_values_from_plugin_become_strings() -> None:
    marker = object()
    run = _summary({"model": marker})
    assert run.meta == {"model": str(marker)}
