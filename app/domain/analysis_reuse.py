"""Решение о переносе результата прошлого анализа без вызова ML.

Вход анализа описывается одним хэшем: версия контракта, провайдер, байты файла
документа и снимки источников. Совпадение хэша с прошлой полной задачей того же
документа означает, что модель получила бы тот же запрос, и её прошлый ответ можно
перенести вместе с решениями пользователя.

Хэш не видит изменений разбора, эвристик и модели внутри harness. Для этого случая
запуск со strategy=full всегда вызывает ML.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class IncrementalMode(StrEnum):
    OFF = "off"
    REUSE = "reuse"
    # В D1 ведёт себя как reuse и дополнительно считает изменённые сегменты.
    DELTA = "delta"


class AnalysisStrategy(StrEnum):
    AUTO = "auto"
    FULL = "full"


class ReuseReason(StrEnum):
    """Почему задача вызвала ML или перенесла результат; пишется в meta задачи."""

    REUSED = "reused"
    MODE_OFF = "mode_off"
    STRATEGY_FULL = "strategy_full"
    SOURCES_FAILED = "sources_failed"
    NO_BASELINE = "no_baseline"
    INPUT_CHANGED = "input_changed"


@dataclass(frozen=True, slots=True)
class Baseline:
    """Прошлая полная задача документа: успешная, без упавших блоков и источников."""

    job_id: uuid.UUID
    input_hash: str


@dataclass(frozen=True, slots=True)
class ReuseDecision:
    reason: ReuseReason
    baseline: Baseline | None

    @property
    def reuse(self) -> bool:
        return self.reason is ReuseReason.REUSED


def analysis_input_hash(
    *,
    contract_version: str,
    provider: str,
    document_hash: str,
    sources: Iterable[tuple[str, str]],
) -> str:
    """sha256 входа; пары (имя источника, хэш снимка) сортируются.

    Порядок источников в запросе задаёт пользователь при запуске, и от перестановки
    ответ модели по существу не меняется. Имя входит в хэш, потому что модель видит его
    и цитирует в основаниях. JSON вместо склейки строк исключает совпадение разных
    входов при именах с разделителями.
    """
    payload = json.dumps(
        [contract_version, provider, document_hash, sorted(sources)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decide_reuse(
    *,
    mode: IncrementalMode,
    strategy: AnalysisStrategy,
    input_hash: str,
    baseline: Baseline | None,
    failed_sources: int,
) -> ReuseDecision:
    """Перенос возможен, только если текущий вход полный и совпал с прошлым.

    Упавший источник меняет вход по сути: прошлый результат строился с ним, и перенос
    выдал бы правки, которые сейчас нечем подтвердить.
    """
    if mode is IncrementalMode.OFF:
        reason = ReuseReason.MODE_OFF
    elif strategy is AnalysisStrategy.FULL:
        reason = ReuseReason.STRATEGY_FULL
    elif failed_sources:
        reason = ReuseReason.SOURCES_FAILED
    elif baseline is None:
        reason = ReuseReason.NO_BASELINE
    elif baseline.input_hash != input_hash:
        reason = ReuseReason.INPUT_CHANGED
    else:
        reason = ReuseReason.REUSED
    return ReuseDecision(reason=reason, baseline=baseline)


@dataclass(frozen=True, slots=True)
class RunStats:
    """Объём входа и отправленного в ML; рядом счётчики вызовов harness, если он их дал."""

    segments_total: int
    segments_sent: int
    chars_total: int
    chars_sent: int
    blocks_total: int
    blocks_checked: int

    def as_meta(self) -> dict[str, int]:
        return {
            "segments_total": self.segments_total,
            "segments_sent": self.segments_sent,
            "chars_total": self.chars_total,
            "chars_sent": self.chars_sent,
            "blocks_total": self.blocks_total,
            "blocks_checked": self.blocks_checked,
        }


# Счётчики harness, которые сводка показывает рядом с нашими; состав meta контрактом не
# зафиксирован, поэтому отсутствующий или нечисловой счётчик пропускается.
HARNESS_COUNTERS = ("quick", "full", "conflict", "add_checks", "llm_seconds")


def harness_counters(meta: Mapping[str, Any] | None) -> dict[str, float]:
    if not meta:
        return {}
    counters: dict[str, float] = {}
    for key in HARNESS_COUNTERS:
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
            continue
        counters[key] = value
    return counters


@dataclass(frozen=True, slots=True)
class DeltaSummary:
    """Сколько входа изменилось относительно прошлой задачи; основа выборочной отправки."""

    changed_share: float
    segments_added: int
    segments_removed: int


@dataclass(frozen=True, slots=True)
class RunInput:
    """Вход задачи и решение по нему; задача хранит это в pipeline_meta."""

    input_hash: str
    contract_version: str
    provider: str
    decision: ReuseDecision
    stats: RunStats
    delta: DeltaSummary | None = None

    def incremental_meta(self) -> dict[str, Any]:
        baseline = self.decision.baseline
        meta: dict[str, Any] = {
            "mode": "reused" if self.decision.reuse else "full",
            "reason": self.decision.reason.value,
            "baseline_job_id": str(baseline.job_id) if baseline else None,
        }
        if self.delta is not None:
            meta["changed_share"] = round(self.delta.changed_share, 4)
            meta["segments_added"] = self.delta.segments_added
            meta["segments_removed"] = self.delta.segments_removed
        return meta

    def run_stats_meta(self, harness_meta: Mapping[str, Any] | None) -> dict[str, Any]:
        stats: dict[str, Any] = self.stats.as_meta()
        counters = harness_counters(harness_meta)
        if counters:
            stats["harness"] = counters
        return stats
