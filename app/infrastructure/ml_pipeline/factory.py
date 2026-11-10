"""Выбор DocumentAnalyzer и лимитов запроса по настройкам ANALYSIS_*/ML_*."""

from collections.abc import Awaitable, Callable

from app.core.config import Settings, get_settings
from app.domain.interfaces.document_analyzer import DocumentAnalyzer
from app.domain.services.analysis_request_builder import AnalysisLimits
from app.infrastructure.llm.factory import get_llm_client
from app.infrastructure.ml_pipeline.http_analyzer import HttpAnalyzer
from app.infrastructure.ml_pipeline.plugin_analyzer import PluginAnalyzer
from app.infrastructure.ml_pipeline.schemas import CONTRACT_VERSION
from app.infrastructure.ml_pipeline.stub_analyzer import StubAnalyzer


def uses_document_analyzer(settings: Settings | None = None) -> bool:
    """legacy_llm идёт прежним конвейером через LLMClient, остальные — через порт 2.0."""
    return (settings or get_settings()).analysis_provider != "legacy_llm"


def get_document_analyzer(settings: Settings | None = None) -> DocumentAnalyzer:
    settings = settings or get_settings()
    provider = settings.analysis_provider
    if provider == "stub":
        return StubAnalyzer()
    if provider == "ml_plugin":
        return PluginAnalyzer(settings.ml_plugin_entrypoint, wait_seconds=settings.ml_wait_seconds)
    if provider == "ml_http":
        return HttpAnalyzer(
            settings.ml_endpoint,
            api_key=settings.ml_api_key,
            mode=settings.ml_http_mode,
            poll_seconds=settings.ml_poll_seconds,
            wait_seconds=settings.ml_wait_seconds,
            max_retries=settings.ml_max_retries,
            health_url=settings.ml_health_url,
        )
    raise ValueError(f"ANALYSIS_PROVIDER={provider} не использует DocumentAnalyzer")


def analysis_limits(settings: Settings | None = None) -> AnalysisLimits:
    settings = settings or get_settings()
    return AnalysisLimits(
        max_blocks=settings.analysis_max_blocks,
        max_block_chars=settings.analysis_max_block_chars,
        max_sources=settings.analysis_max_sources,
    )


def contract_version(settings: Settings | None = None) -> str | None:
    """Версия контракта ML, по которому идёт анализ; legacy_llm работает без контракта."""
    return CONTRACT_VERSION if uses_document_analyzer(settings) else None


async def _unknown() -> bool | None:
    return None


def pipeline_health_check(settings: Settings | None = None) -> Callable[[], Awaitable[bool | None]]:
    """Проверка конвейера из процесса API, без запуска анализа.

    ml_plugin установлен только в образ воркера: адаптер импортирует entrypoint при
    создании, и в API этого пакета может не быть. Поэтому проверка — «неизвестно».
    """
    settings = settings or get_settings()
    if settings.analysis_provider == "legacy_llm":
        return get_llm_client(settings).health_check
    if settings.analysis_provider == "ml_plugin":
        return _unknown
    return get_document_analyzer(settings).health_check
