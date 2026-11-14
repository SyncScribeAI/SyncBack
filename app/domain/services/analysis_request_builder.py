"""Сборка запроса к ML-конвейеру из блоков документа и текстов источников.

Чистая функция без ввода-вывода: тексты источников уже загружены, и лимит длины
источника проверен сразу после загрузки — чтобы не держать в памяти источник, который
всё равно не уйдёт в запрос. Здесь проверяются лимиты, которые видны
только на уровне всего запроса: число блоков, длина блока, число источников.
"""

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from app.domain.exceptions import LLMInputTooLargeError
from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    BlockPayload,
    DocumentPayload,
    SourcePayload,
)
from app.domain.interfaces.document_parser import ParsedBlock


@dataclass(frozen=True, slots=True)
class AnalysisLimits:
    """None — без ограничения. Значения приходят из настроек; домен окружение не читает."""

    max_blocks: int | None = None
    max_block_chars: int | None = None
    max_sources: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentInput:
    id: uuid.UUID
    name: str
    format: str
    plain_text: str
    blocks: Iterable[ParsedBlock]


def build_analysis_request(
    *,
    job_id: uuid.UUID,
    attempt: int,
    document: DocumentInput,
    sources: Iterable[SourcePayload],
    limits: AnalysisLimits = AnalysisLimits(),
    editable: Collection[str] | None = None,
) -> AnalysisRequest:
    """O(B + K): один проход по блокам и источникам, уникальность ref — через set.

    request_id включает номер попытки: ответ на прошлую попытку, пришедший с опозданием,
    не будет принят за ответ на текущую. editable сужает проверяемые блоки до выбранных
    инкрементальным анализом; остальные уходят контекстом, чтобы модель видела документ
    целиком.
    """
    blocks = _block_payloads(document, limits, editable)
    source_payloads = tuple(sources)
    if limits.max_sources is not None and len(source_payloads) > limits.max_sources:
        raise LLMInputTooLargeError(
            f"Источников {len(source_payloads)}, допустимо не более {limits.max_sources}"
        )
    return AnalysisRequest(
        request_id=f"{job_id}:{attempt}",
        analysis_job_id=str(job_id),
        document=DocumentPayload(
            id=str(document.id), name=document.name, format=document.format, blocks=blocks
        ),
        sources=source_payloads,
    )


def _block_payloads(
    document: DocumentInput, limits: AnalysisLimits, editable: Collection[str] | None
) -> tuple[BlockPayload, ...]:
    seen: set[str] = set()
    payloads: list[BlockPayload] = []
    for block in document.blocks:
        if block.ref in seen:
            raise ValueError(f"Повторяющийся ref блока: {block.ref}")
        seen.add(block.ref)
        length = block.end_offset - block.start_offset
        # Длину проверяем по смещениям до среза: слишком длинный блок не копируется.
        if limits.max_block_chars is not None and length > limits.max_block_chars:
            raise LLMInputTooLargeError(
                f"Блок {block.ref} длиной {length} символов, "
                f"допустимо не более {limits.max_block_chars}"
            )
        payloads.append(
            BlockPayload(
                ref=block.ref,
                type=block.type,
                text=block.text_in(document.plain_text),
                editable=block.editable and (editable is None or block.ref in editable),
                start_offset=block.start_offset,
            )
        )
        if limits.max_blocks is not None and len(payloads) > limits.max_blocks:
            raise LLMInputTooLargeError(
                f"В документе больше {limits.max_blocks} блоков — допустимый предел запроса"
            )
    return tuple(payloads)
