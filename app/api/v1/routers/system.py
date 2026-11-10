"""Служебные эндпоинты диагностики окружения и возможностей системы."""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends

from app.api.deps import require_admin
from app.api.schemas.system import (
    AnalysisCapabilities,
    AnalysisHealthResponse,
    AnalysisQueueHealth,
    CapabilitiesResponse,
    ExportCapabilities,
    ReviewCapabilities,
    UploadCapabilities,
)
from app.core.config import Settings, get_settings
from app.core.dependencies import (
    get_analysis_queue_instance,
    get_llm_client_instance,
    get_pipeline_health_check,
)
from app.domain.analysis_health import AnalysisHealth
from app.domain.interfaces.analysis_queue import AnalysisQueue
from app.domain.interfaces.llm_client import LLMClient
from app.domain.services.document_service import FORMAT_BY_EXTENSION
from app.domain.value_objects import DocumentStatusVO
from app.infrastructure.db.models.user import User
from app.infrastructure.ml_pipeline.factory import contract_version

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/llm-health", deprecated=True)
async def llm_health(
    current_user: User = Depends(require_admin),
    llm_client: LLMClient = Depends(get_llm_client_instance),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Диагностика LLM-провайдера прежнего анализа; замена — /system/analysis-health.

    Только для администратора, раскрывает конфигурацию. Ответ не меняется, чтобы не
    сломать тех, кто его уже читает.
    """
    is_healthy = await llm_client.health_check()
    return {"provider": settings.llm_provider, "healthy": is_healthy}


@router.get("/analysis-health", response_model=AnalysisHealthResponse)
async def analysis_health(
    current_user: User = Depends(require_admin),
    check_pipeline: Callable[[], Awaitable[bool | None]] = Depends(get_pipeline_health_check),
    queue: AnalysisQueue = Depends(get_analysis_queue_instance),
    settings: Settings = Depends(get_settings),
) -> AnalysisHealthResponse:
    """Готов ли анализ: доступен ли конвейер и слушает ли кто-нибудь очередь задач.

    Только для администратора: раскрывает провайдера и имя очереди. Обе проверки сетевые
    и независимые, поэтому идут параллельно.
    """
    healthy, probe = await asyncio.gather(check_pipeline(), queue.probe())
    health = AnalysisHealth(
        provider=settings.analysis_provider,
        contract_version=contract_version(settings),
        pipeline_healthy=healthy,
        queue=probe,
    )
    return AnalysisHealthResponse(
        status=health.status.value,
        provider=health.provider,
        contract_version=health.contract_version,
        healthy=health.pipeline_healthy,
        queue=AnalysisQueueHealth(name=probe.queue, consumers=probe.consumers),
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(settings: Settings = Depends(get_settings)) -> CapabilitiesResponse:
    """Форматы, лимиты и правила API для фронтенда.

    Публичный: нужен до входа, чтобы настроить загрузку. Значения берутся из тех же
    настроек и таблиц, что проверяют запросы, поэтому не расходятся с поведением.
    """
    document_base = "/api/v1/projects/{project_id}/documents/{document_id}"
    return CapabilitiesResponse(
        upload=UploadCapabilities(
            max_size_mb=settings.max_upload_size_mb,
            max_size_bytes=settings.max_upload_size_bytes,
            document_formats=sorted({f.value for f in FORMAT_BY_EXTENSION.values()}),
            document_extensions=sorted(FORMAT_BY_EXTENSION),
            unsupported_extensions=[".doc"],
        ),
        analysis=AnalysisCapabilities(
            idempotency_header="Idempotency-Key",
            parallel_jobs_per_document=1,
            force_required_for_ready=True,
            contract_version=contract_version(settings),
        ),
        review=ReviewCapabilities(
            atomic_save_endpoint=f"PUT {document_base}/suggestions/review",
            optimistic_locking=True,
            version_header="If-Match",
        ),
        export=ExportCapabilities(
            endpoint=f"GET {document_base}/export",
            same_format_only=True,
            requires_status=DocumentStatusVO.READY.value,
        ),
    )
