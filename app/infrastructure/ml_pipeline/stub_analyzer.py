"""Заглушка ML-конвейера для dev-окружения и тестов фронтенда.

Правки строятся по реальным блокам запроса, чтобы проходить те же проверки дословности,
что и ответ настоящего конвейера: modify первого редактируемого блока целиком и add
после второго. Ответ собирается в JSON контракта и разбирается той же схемой — так
заглушка проверяет и путь разбора, а не только задачу анализа.
"""

from typing import Any

from app.domain.interfaces.document_analyzer import AnalysisRequest, AnalysisResult
from app.infrastructure.ml_pipeline.schemas import CONTRACT_VERSION, decode_response

_MARK = " (проверено заглушкой ML)"


class StubAnalyzer:
    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        return decode_response(
            stub_response(request),
            expected_request_id=request.request_id,
            source_names=[source.name for source in request.sources],
        )

    async def health_check(self) -> bool:
        return True


def stub_response(request: AnalysisRequest) -> dict[str, Any]:
    editable = [block for block in request.document.blocks if block.editable]
    suggestions: list[dict[str, Any]] = []
    if editable:
        first = editable[0]
        suggestions.append(
            {
                "suggestion_id": "s-1",
                "change_type": "modify",
                "target_block_id": first.ref,
                "old_text": first.text,
                "new_text": first.text + _MARK,
                "insert_after": None,
                "rationale": "Пример правки заглушки ML-конвейера.",
                "confidence_score": 0.95,
            }
        )
    if len(editable) > 1:
        second = editable[1]
        suggestions.append(
            {
                "suggestion_id": "s-2",
                "change_type": "add",
                "target_block_id": second.ref,
                "old_text": None,
                "new_text": "Пример добавления заглушки ML-конвейера.",
                "insert_after": second.text,
                "rationale": "Пример добавления; уверенность ниже порога подсветки.",
                "confidence_score": 0.6,
            }
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request.request_id,
        "analysis_job_id": request.analysis_job_id,
        "status": "completed",
        "suggestions": suggestions,
        "flags": [],
        "meta": {"blocks": len(request.document.blocks), "checked_blocks": len(editable)},
        "error": None,
    }
