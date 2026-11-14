"""Решение о переносе результата прошлого анализа без вызова ML.

Вход анализа описывается одним хэшем: версия контракта, провайдер, байты файла
документа и источники вместе с их идентичностью и снимками. Совпадение хэша с прошлой полной задачей того же
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
    # Только перенос результата при неизменном входе.
    REUSE = "reuse"
    # Перенос при неизменном входе и выборочная проверка блоков при изменённых источниках.
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


class DeltaFallback(StrEnum):
    """Почему изменённый вход проверен полным анализом, а не выборочно; пишется в meta."""

    MODE_REUSE = "mode_reuse"
    # Прошлая задача записана до инкрементального режима: неизвестен хэш её документа.
    BASELINE_UNKNOWN = "baseline_unknown"
    DOCUMENT_CHANGED = "document_changed"
    CONTRACT_CHANGED = "contract_changed"
    PROVIDER_CHANGED = "provider_changed"
    CHAIN_LIMIT = "chain_limit"
    FINGERPRINTS_UNAVAILABLE = "fingerprints_unavailable"
    CHANGED_SHARE = "changed_share"
    BLOCKS_SHARE = "blocks_share"
    # Новый текст источника не совпал ни с одним блоком: правка add возможна где угодно.
    NO_BLOCKS_MATCHED = "no_blocks_matched"
    # Источник прошлой задачи удалён, и появился новый: основания перенесённых правок
    # указывали бы на удалённый источник, даже если текст нового совпадает.
    SOURCES_REPLACED = "sources_replaced"


@dataclass(frozen=True, slots=True)
class Baseline:
    """Прошлая полная задача документа: успешная, без упавших блоков и источников.

    chain_length: сколько инкрементальных задач подряд стоит за ней после последнего
    полного анализа. Поля входа пусты у задач, записанных до инкрементального режима.
    """

    job_id: uuid.UUID
    input_hash: str
    document_hash: str | None = None
    contract_version: str | None = None
    provider: str | None = None
    chain_length: int = 0


@dataclass(frozen=True, slots=True)
class DeltaPolicy:
    """Пределы, за которыми выборочная проверка теряет смысл; из ANALYSIS_DELTA_*."""

    max_changed_share: float = 0.3
    max_blocks_share: float = 0.5
    max_chain: int = 5


def delta_precheck(
    baseline: Baseline,
    *,
    mode: IncrementalMode,
    document_hash: str,
    contract_version: str,
    provider: str,
    policy: DeltaPolicy,
) -> DeltaFallback | None:
    """Условия, проверяемые до загрузки отпечатков прошлого входа.

    Изменённый документ, контракт или провайдер меняют смысл всех блоков сразу, и
    сравнивать источники уже незачем. Предел цепочки ограничивает накопление пропусков:
    каждая выборочная задача наследует результат предыдущей.
    """
    if mode is not IncrementalMode.DELTA:
        return DeltaFallback.MODE_REUSE
    if baseline.document_hash is None:
        return DeltaFallback.BASELINE_UNKNOWN
    if baseline.document_hash != document_hash:
        return DeltaFallback.DOCUMENT_CHANGED
    if baseline.contract_version != contract_version:
        return DeltaFallback.CONTRACT_CHANGED
    if baseline.provider != provider:
        return DeltaFallback.PROVIDER_CHANGED
    if baseline.chain_length >= policy.max_chain:
        return DeltaFallback.CHAIN_LIMIT
    return None


def sources_replaced_check(
    baseline_ids: Iterable[uuid.UUID], current_ids: Iterable[uuid.UUID]
) -> DeltaFallback | None:
    """Пропажа и появление источников одновременно: перенос сохранил бы чужие основания.

    Только удаление или только добавление выборочная проверка обрабатывает сама:
    правки без основания помечаются, новый текст уходит в модель.
    """
    baseline, current = set(baseline_ids), set(current_ids)
    if baseline - current and current - baseline:
        return DeltaFallback.SOURCES_REPLACED
    return None


def delta_scope_check(
    *, changed_share: float, selected: int, checkable: int, policy: DeltaPolicy
) -> DeltaFallback | None:
    """Большая доля изменений или выбранных блоков: полный анализ надёжнее и не дороже."""
    if changed_share > policy.max_changed_share:
        return DeltaFallback.CHANGED_SHARE
    if checkable and selected > checkable * policy.max_blocks_share:
        return DeltaFallback.BLOCKS_SHARE
    return None


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
    sources: Iterable[tuple[str, str, str, str]],
) -> str:
    """sha256 входа; четвёрки (id источника, тип, имя, хэш снимка) сортируются.

    Порядок источников в запросе задаёт пользователь при запуске, и от перестановки
    ответ модели по существу не меняется. Имя входит в хэш, потому что модель видит его
    и цитирует в основаниях. Id и тип входят, потому что основания перенесённых правок
    ссылаются на конкретный источник: пересозданный источник с тем же текстом должен
    дать новый вход. JSON вместо склейки строк исключает совпадение разных входов при
    именах с разделителями.
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
    """Вход задачи и решение по нему; задача хранит это в pipeline_meta.

    selective: задача проверила заново только часть блоков, остальное перенесено из
    baseline. selection: счётчики отбора и сведения для meta.
    """

    input_hash: str
    contract_version: str
    provider: str
    decision: ReuseDecision
    stats: RunStats
    delta: DeltaSummary | None = None
    document_hash: str | None = None
    selective: bool = False
    fallback: DeltaFallback | None = None
    selection: Mapping[str, int] | None = None

    @property
    def mode(self) -> str:
        if self.decision.reuse:
            return "reused"
        return "delta" if self.selective else "full"

    @property
    def chain_length(self) -> int:
        """Перенос не прибавляет звена: результат тот же, что у baseline."""
        baseline = self.decision.baseline
        if baseline is None or self.mode == "full":
            return 0
        return baseline.chain_length + (1 if self.selective else 0)

    @property
    def baseline_job_id(self) -> uuid.UUID | None:
        baseline = self.decision.baseline
        return baseline.job_id if baseline is not None and self.mode != "full" else None

    def incremental_meta(self) -> dict[str, Any]:
        baseline = self.decision.baseline
        fallback: str | None = None
        if self.mode == "full":
            fallback = self.fallback.value if self.fallback else self.decision.reason.value
        meta: dict[str, Any] = {
            "mode": self.mode,
            "reason": self.decision.reason.value,
            "baseline_job_id": str(baseline.job_id) if baseline else None,
            "chain_length": self.chain_length,
            "fallback_reason": fallback,
            "document_hash": self.document_hash,
        }
        if self.delta is not None:
            meta["changed_share"] = round(self.delta.changed_share, 4)
            meta["segments_added"] = self.delta.segments_added
            meta["segments_removed"] = self.delta.segments_removed
        if self.selection:
            meta.update(self.selection)
        return meta

    def run_stats_meta(self, harness_meta: Mapping[str, Any] | None) -> dict[str, Any]:
        stats: dict[str, Any] = self.stats.as_meta()
        counters = harness_counters(harness_meta)
        if counters:
            stats["harness"] = counters
        return stats
