"""Схема /system/capabilities: всё, что фронт читает при старте вместо хардкода."""

from typing import Literal

from pydantic import BaseModel, Field


class UploadCapabilities(BaseModel):
    max_size_mb: int = Field(description="Максимальный размер файла, МБ (документы и источники)")
    max_size_bytes: int
    document_formats: list[str] = Field(
        description="Значения поля document.format, которые может получить загруженный документ"
    )
    document_extensions: list[str] = Field(
        description="Расширения файлов документа, которые принимает загрузка (с точкой, строчные)"
    )
    source_extensions: list[str] | None = Field(
        default=None,
        description=(
            "Расширения файлов источника, которые принимает загрузка (с точкой, строчные); "
            "форма источника читает их вместо document_extensions"
        ),
    )
    unsupported_extensions: list[str] = Field(
        description="Известные расширения, на которые загрузка ответит 415"
    )


class AnalysisCapabilities(BaseModel):
    idempotency_header: str = Field(description="Необязательный заголовок для повтора запуска")
    parallel_jobs_per_document: int
    force_required_for_ready: bool = Field(
        description="Повторный анализ готового документа требует force=true, иначе 409"
    )
    contract_version: str | None = Field(
        default=None,
        description=(
            "Версия контракта ML-конвейера, по которому идёт анализ; null — прежний анализ "
            "без контракта (блоки, группы правок и флаги не заполняются)"
        ),
    )


class ReviewCapabilities(BaseModel):
    atomic_save_endpoint: str
    optimistic_locking: bool
    version_header: str = Field(description="Заголовок с review_version; при расхождении 412")


class ExportCapabilities(BaseModel):
    endpoint: str
    same_format_only: bool = Field(description="Экспорт только в исходном формате документа")
    requires_status: str


class CapabilitiesResponse(BaseModel):
    """Возможности бэкенда, которые фронт читает один раз при старте."""

    upload: UploadCapabilities
    analysis: AnalysisCapabilities
    review: ReviewCapabilities
    export: ExportCapabilities


class AnalysisQueueHealth(BaseModel):
    name: str = Field(description="Очередь Celery, в которую уходит анализ")
    consumers: int | None = Field(
        description="Сколько воркеров слушают очередь; null — брокер не ответил"
    )


class AnalysisHealthResponse(BaseModel):
    """Готовность анализа: конвейер и воркеры очереди. Только для администратора."""

    status: Literal["ok", "down", "unknown"] = Field(
        description=(
            "down — конвейер недоступен или очередь никто не слушает; unknown — что-то "
            "проверить не удалось"
        )
    )
    provider: str = Field(description="ANALYSIS_PROVIDER")
    contract_version: str | None
    healthy: bool | None = Field(
        description="Ответ проверки конвейера; null — проверить нечем без запуска анализа"
    )
    queue: AnalysisQueueHealth
