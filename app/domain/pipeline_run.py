"""Сводка вызова ML-конвейера, которую задача анализа хранит рядом со своим статусом.

request_id связывает задачу с логами harness; checked_blocks и failed_blocks вынесены из
meta в отдельные поля, чтобы фронт показывал «проверено N из M» без разбора JSON. Состав
meta контрактом не зафиксирован, поэтому счётчики читаются мягко: нечисловое значение —
как отсутствие.

meta приходит от внешней системы, и её размер мы не контролируем. Слишком большая meta
не сохраняется целиком: строка задачи читается на каждый опрос статуса.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.interfaces.document_analyzer import AnalysisResult

META_MAX_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class PipelineRunSummary:
    request_id: str
    contract_version: str
    provider: str
    # None — meta не сохранена: больше META_MAX_BYTES или не представима в JSON.
    meta: Mapping[str, Any] | None
    checked_blocks: int | None
    failed_blocks: int | None
    # Счётчики оснований (EvidenceCounters.as_meta); None, пока локатор не отработал.
    evidence: Mapping[str, int] | None = None

    @property
    def meta_dropped(self) -> bool:
        return self.meta is None


def summarize_run(
    result: AnalysisResult, *, request_id: str, contract_version: str, provider: str
) -> PipelineRunSummary:
    """O(размер meta): сериализация для проверки размера и обратный разбор.

    Обратный разбор нужен, чтобы в JSONB ушли только JSON-значения: адаптер-плагин может
    положить в meta что угодно, и падать на записи результата из-за служебных данных нельзя.
    NaN и бесконечности PostgreSQL в JSONB не принимает — такая meta тоже не сохраняется.
    """
    meta: Any = None
    try:
        encoded = json.dumps(result.meta, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        encoded = None
    if encoded is not None and len(encoded.encode()) <= META_MAX_BYTES:
        meta = json.loads(encoded)
    return PipelineRunSummary(
        request_id=request_id,
        contract_version=contract_version,
        provider=provider,
        meta=meta,
        checked_blocks=_counter(result.meta, "checked_blocks"),
        failed_blocks=_counter(result.meta, "errors"),
    )


def failed_block_count(meta: Mapping[str, Any]) -> int:
    """Число блоков, которые конвейер не смог проверить (meta.errors); 0, если неизвестно."""
    return _counter(meta, "errors") or 0


def _counter(meta: Mapping[str, Any], key: str) -> int | None:
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return None
    if value in (float("inf"), float("-inf")):
        return None
    return max(int(value), 0)
