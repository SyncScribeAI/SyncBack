"""JSON контракта ML 2.0 ↔ доменные DTO.

Единственное место, где живут имена полей контракта: домен и воркер работают с
block_ref/anchor_text/ReviewFlag, и смена версии контракта меняет только этот модуль.

Ответ проверяется строго там, где от этого зависит корректность (версия, статус,
обязательные поля), и мягко там, где ML может расширяться: лишние поля игнорируются,
неизвестный change_type доходит до доменного маппера и отбрасывается одной правкой, а не
всем ответом; неизвестный код ошибки считается временным сбоем.

Память: запрос сериализуется один раз в bytes (те же байты уходят в адаптер и в архив
payload), ответ разбирается за один проход model_validate_json без промежуточного дерева
json.loads.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal

import pydantic_core
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.evidence import Quote
from app.domain.exceptions import LLMInvalidResponseError
from app.domain.interfaces.document_analyzer import (
    AnalysisError,
    AnalysisErrorCode,
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    ProposedChange,
    ReviewFlag,
)
from app.infrastructure.ml_pipeline.rationale_quotes import parse_rationale_quotes

CONTRACT_VERSION = "2.0"
_KNOWN_ERROR_CODES = frozenset(code.value for code in AnalysisErrorCode)


def to_contract(request: AnalysisRequest) -> dict[str, Any]:
    """Словарь запроса для плагина. Строки не копируются — словарь ссылается на тексты DTO.

    contract_version в запросе контракт не требует (лишние поля игнорируются), но с ним
    ML-сторона сможет отличить версии, когда начнёт его проверять.
    """
    document = request.document
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request.request_id,
        "analysis_job_id": request.analysis_job_id,
        "document": {
            "id": document.id,
            "name": document.name,
            "format": document.format,
            "blocks": [
                {
                    "block_id": block.ref,
                    "type": block.type.value,
                    "text": block.text,
                    "editable": block.editable,
                }
                for block in document.blocks
            ],
        },
        "sources": [
            {"id": source.id, "name": source.name, "type": source.type.value, "text": source.text}
            for source in request.sources
        ],
    }


def encode_request(request: AnalysisRequest) -> bytes:
    """UTF-8 JSON запроса одним буфером; кириллица не экранируется — 2 байта на символ, а не 6."""
    return pydantic_core.to_json(to_contract(request))


class _Schema(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _QuoteSchema(_Schema):
    source: str = ""
    source_id: str | None = None
    text: str = ""

    def to_quote(self) -> Quote:
        return Quote(source_name=self.source, text=self.text, source_id=self.source_id)


class _SuggestionSchema(_Schema):
    change_type: str
    target_block_id: str
    old_text: str | None = None
    new_text: str | None = None
    insert_after: str | None = None
    rationale: str | None = None
    confidence_score: float | None = None
    # В контракте 2.0 поля нет; когда harness начнёт его отдавать, оно важнее разбора
    # строки обоснования.
    quotes: list[_QuoteSchema] | None = None

    def to_quotes(self, source_names: Sequence[str]) -> tuple[Quote, ...]:
        if self.quotes is not None:
            return tuple(quote.to_quote() for quote in self.quotes if quote.text.strip())
        return parse_rationale_quotes(self.rationale, source_names)


class _FlagSchema(_Schema):
    reason: str = ""
    target_block_id: str
    old_text: str = ""
    rationale: str | None = None
    quotes: list[_QuoteSchema] = Field(default_factory=list)


class _ErrorSchema(_Schema):
    code: str = AnalysisErrorCode.PIPELINE_ERROR.value
    message: str = ""


class _ResponseSchema(_Schema):
    contract_version: Literal["2.0"]
    request_id: str | None = None
    status: Literal["completed", "failed"]
    suggestions: list[_SuggestionSchema] = Field(default_factory=list)
    flags: list[_FlagSchema] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    error: _ErrorSchema | None = None


def decode_response(
    raw: bytes | str | Mapping[str, Any],
    *,
    expected_request_id: str | None = None,
    source_names: Sequence[str] = (),
) -> AnalysisResult:
    """Ответ ML → AnalysisResult; ответ не по контракту → LLMInvalidResponseError.

    request_id сверяется, чтобы опоздавший ответ на прошлую попытку не был принят за
    текущий. Отсутствующий request_id допустим: контракт возвращает null, если его не было.
    source_names помогают разобрать цитаты в обосновании: имя источника может содержать
    скобки, и без списка имён граница цитаты неоднозначна.
    """
    try:
        if isinstance(raw, bytes | str):
            parsed = _ResponseSchema.model_validate_json(raw)
        else:
            parsed = _ResponseSchema.model_validate(raw)
    except ValidationError as exc:
        raise LLMInvalidResponseError(_describe(exc)) from None
    if (
        expected_request_id is not None
        and parsed.request_id is not None
        and parsed.request_id != expected_request_id
    ):
        raise LLMInvalidResponseError("Ответ ML относится к другому запросу (request_id)")
    return _to_result(parsed, source_names)


def _describe(exc: ValidationError) -> str:
    """Без входных значений: в них может оказаться текст документа или источника."""
    errors = exc.errors(include_input=False, include_url=False)
    first = errors[0]
    location = ".".join(str(part) for part in first["loc"]) or "<корень>"
    return f"Ответ ML не по контракту {CONTRACT_VERSION}: ошибок {len(errors)}, первая — {location} ({first['type']})"


def _to_result(parsed: _ResponseSchema, source_names: Sequence[str]) -> AnalysisResult:
    if parsed.status == "failed":
        error = parsed.error or _ErrorSchema()
        code = (
            AnalysisErrorCode(error.code)
            if error.code in _KNOWN_ERROR_CODES
            else AnalysisErrorCode.PIPELINE_ERROR
        )
        return AnalysisResult(
            status=AnalysisStatus.FAILED,
            meta=parsed.meta,
            error=AnalysisError(code=code, message=error.message),
        )
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=tuple(
            ProposedChange(
                kind=item.change_type,
                block_ref=item.target_block_id,
                old_text=item.old_text,
                new_text=item.new_text,
                anchor_text=item.insert_after,
                rationale=item.rationale,
                confidence=item.confidence_score,
                quotes=item.to_quotes(source_names),
            )
            for item in parsed.suggestions
        ),
        flags=tuple(
            ReviewFlag(
                reason=flag.reason,
                block_ref=flag.target_block_id,
                block_text=flag.old_text,
                rationale=flag.rationale,
                quotes=tuple(quote.to_quote() for quote in flag.quotes),
            )
            for flag in parsed.flags
        ),
        meta=parsed.meta,
    )
