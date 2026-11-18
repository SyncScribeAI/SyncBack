"""Расход токенов модели по обращениям одной задачи анализа.

Счётчик, который провайдер не вернул, хранится как None, а не как ноль: ноль занизил бы
расход и сделал бы задачу дешевле, чем она была. Итог с неизвестным счётчиком помечается
неполным, и сравнивать его с другими задачами можно только с этой оговоркой.

Reasoning-токены у OpenAI-совместимых API входят в completion_tokens, поэтому в итог
выхода они повторно не прибавляются, а хранятся отдельно для разбора.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LlmCall:
    """Одно обращение к модели. stage — шаг конвейера: quick, full, direct и т. п."""

    stage: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    input_chars: int | None = None
    attempt: int = 0
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class LlmUsage:
    calls: tuple[LlmCall, ...] = ()

    @property
    def complete(self) -> bool:
        """Все обращения вернули входные и выходные токены."""
        return all(
            call.input_tokens is not None and call.output_tokens is not None for call in self.calls
        )

    @property
    def input_tokens(self) -> int:
        return sum(call.input_tokens or 0 for call in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(call.output_tokens or 0 for call in self.calls)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merged(self, other: LlmUsage | None) -> LlmUsage:
        """Расход двух прогонов одной задачи: повтор, аудит или переход к полному анализу."""
        if other is None:
            return self
        return LlmUsage(self.calls + other.calls)

    def as_meta(self) -> dict[str, Any]:
        """Итог для pipeline_meta: небольшой и не зависит от размера meta конвейера."""
        return {
            "calls": len(self.calls),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": sum(call.reasoning_tokens or 0 for call in self.calls),
            "cached_input_tokens": sum(call.cached_input_tokens or 0 for call in self.calls),
            "complete": self.complete,
            "by_stage": _by_stage(self.calls),
        }


def _by_stage(calls: Sequence[LlmCall]) -> dict[str, dict[str, int]]:
    stages: dict[str, dict[str, int]] = {}
    for call in calls:
        item = stages.setdefault(call.stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        item["calls"] += 1
        item["input_tokens"] += call.input_tokens or 0
        item["output_tokens"] += call.output_tokens or 0
    return stages


def usage_from_records(records: Iterable[Mapping[str, Any]]) -> LlmUsage:
    """Записи meta.llm_calls конвейера → LlmUsage; нечисловой счётчик считается неизвестным."""
    calls: list[LlmCall] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        calls.append(
            LlmCall(
                stage=str(record.get("stage") or "unknown"),
                model=_text(record.get("model")),
                input_tokens=_count(record.get("input_tokens")),
                output_tokens=_count(record.get("output_tokens")),
                reasoning_tokens=_count(record.get("reasoning_tokens")),
                cached_input_tokens=_count(record.get("cached_input_tokens")),
                input_chars=_count(record.get("input_chars")),
                attempt=_count(record.get("attempt")) or 0,
                status=str(record.get("status") or "ok"),
            )
        )
    return LlmUsage(tuple(calls))


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return None
    if value in (float("inf"), float("-inf")) or value < 0:
        return None
    return int(value)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
