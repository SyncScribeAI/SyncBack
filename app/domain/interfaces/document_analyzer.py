"""Порт анализа документа ML-конвейером (контракт 2.0) и его DTO.

Домен описывает запрос и результат своими терминами (block_ref, anchor_text, ReviewFlag);
имена полей контракта живут только в инфраструктурной схеме, которая переводит
JSON ↔ DTO. Так устройство harness и
версия контракта не протекают в правила маппинга и в задачу воркера.

DTO неизменяемые и со slots: запрос держит тексты всех блоков и источников, и лишний
__dict__ на каждый блок заметен на документах в тысячи блоков.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.interfaces.source_connector import SourceKind


@dataclass(frozen=True, slots=True)
class BlockPayload:
    """Блок в запросе к ML.

    start_offset — начало блока в plain_text документа. В запрос он не уходит: по нему
    маппер переводит фрагмент правки из координат блока в координаты документа.
    """

    ref: str
    type: DocumentBlockType
    text: str
    editable: bool
    start_offset: int = 0

    def __post_init__(self) -> None:
        # Блок без ref или текста конвейер отклонит как invalid_request — это наша ошибка,
        # и ловить её дешевле до вызова, чем по ответу.
        if not self.ref:
            raise ValueError("У блока документа пустой ref")
        if not self.text:
            raise ValueError(f"У блока {self.ref} пустой текст")


@dataclass(frozen=True, slots=True)
class DocumentPayload:
    id: str
    name: str
    format: str
    blocks: tuple[BlockPayload, ...]


@dataclass(frozen=True, slots=True)
class SourcePayload:
    id: str
    name: str
    type: SourceKind
    text: str


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    request_id: str
    analysis_job_id: str
    document: DocumentPayload
    sources: tuple[SourcePayload, ...]


class ChangeKind(StrEnum):
    """Значения совпадают с ChangeType в БД, чтобы перевод был без таблицы соответствий."""

    MODIFY = "modify"
    ADD = "add"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class ProposedChange:
    """Правка, предложенная конвейером.

    old_text — заменяемый фрагмент блока (modify/delete); anchor_text — фрагмент блока,
    после которого вставляется new_text (add). Оба должны дословно входить в текст блока,
    но это проверяет маппер, а не DTO: невалидная правка отбрасывается, а не роняет разбор.
    """

    kind: str
    block_ref: str
    old_text: str | None
    new_text: str | None
    anchor_text: str | None = None
    rationale: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class FlagQuote:
    source_name: str
    text: str


@dataclass(frozen=True, slots=True)
class ReviewFlag:
    """Место, которое человек должен проверить сам: правки нет, есть причина и цитаты."""

    reason: str
    block_ref: str
    block_text: str
    rationale: str | None = None
    quotes: tuple[FlagQuote, ...] = ()


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisErrorCode(StrEnum):
    """INVALID_REQUEST — запрос не по контракту, т. е. наша ошибка, повтор не поможет.

    PIPELINE_ERROR — сбой конвейера или модели; неизвестные коды адаптер сводит сюда же,
    чтобы новый код на стороне ML не превращал временный сбой в окончательный.
    """

    INVALID_REQUEST = "invalid_request"
    PIPELINE_ERROR = "pipeline_error"


@dataclass(frozen=True, slots=True)
class AnalysisError:
    code: AnalysisErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    status: AnalysisStatus
    suggestions: tuple[ProposedChange, ...] = ()
    flags: tuple[ReviewFlag, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)
    error: AnalysisError | None = None


class DocumentAnalyzer(Protocol):
    """Один вызов — весь документ со всеми источниками.

    Реализация отвечает за транспорт и дедлайн; ответ status=failed возвращается как
    AnalysisResult, а не исключением: решение о повторе принимает AnalysisJobLifecycle.
    Исключения — только когда ответа нет вовсе (сеть, таймаут) или он не по контракту.
    """

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...

    async def health_check(self) -> bool | None:
        """Доступность конвейера без запуска анализа. None — проверить нечем."""
        ...
