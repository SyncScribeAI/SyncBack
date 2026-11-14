"""Точка входа FastAPI-приложения."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.v1.routers.analysis_jobs import router as analysis_jobs_router
from app.api.v1.routers.analysis_jobs_bulk import router as analysis_jobs_bulk_router
from app.api.v1.routers.auth import router as auth_router
from app.api.v1.routers.dashboard import router as dashboard_router
from app.api.v1.routers.documents import router as documents_router
from app.api.v1.routers.documents_global import router as documents_global_router
from app.api.v1.routers.editor import router as editor_router
from app.api.v1.routers.flags import router as flags_router
from app.api.v1.routers.my_documents import router as my_documents_router
from app.api.v1.routers.projects import router as projects_router
from app.api.v1.routers.source_files import router as source_files_router
from app.api.v1.routers.source_snapshots import router as source_snapshots_router
from app.api.v1.routers.sources import router as sources_router
from app.api.v1.routers.sse import router as sse_router
from app.api.v1.routers.suggestions import router as suggestions_router
from app.api.v1.routers.system import router as system_router
from app.core.body_size_limit_middleware import BodySizeLimitMiddleware
from app.core.config import get_settings
from app.core.correlation_middleware import CorrelationIdMiddleware
from app.core.dispatch_reconciler import start_dispatch_reconciler, stop_dispatch_reconciler
from app.core.limiter import limiter
from app.core.logging_setup import configure_logging
from app.domain.exceptions import (
    AnalysisJobNotFoundError,
    DocumentNotFoundError,
    DomainError,
    ProjectNotFoundError,
    SourceNotFoundError,
    SuggestionNotFoundError,
)
from app.infrastructure.db.session import dispose_engine
from app.infrastructure.events.sse_broker import init_sse_broker, shutdown_sse_broker

logger = logging.getLogger("syncscribe.main")

# NotFound-исключения домена отдаются как 404, а не общий 400.
_NOT_FOUND_EXCEPTIONS = (
    DocumentNotFoundError,
    ProjectNotFoundError,
    SourceNotFoundError,
    SuggestionNotFoundError,
    AnalysisJobNotFoundError,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    logger.info(
        "Запуск SyncScribe backend",
        extra={"env": settings.env, "llm_provider": settings.llm_provider},
    )

    await init_sse_broker(redis_url=settings.redis_url, channel=settings.redis_sse_channel)
    reconciler = start_dispatch_reconciler(settings)

    try:
        yield
    finally:
        await stop_dispatch_reconciler(reconciler)
        await shutdown_sse_broker()
        await dispose_engine()
        logger.info("Остановка SyncScribe backend")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="SyncScribe API",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    # slowapi limiter — до регистрации роутеров.
    app.state.limiter = limiter
    # Обработчик slowapi объявлен с узким типом исключения, Starlette ждёт Exception.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # Starlette оборачивает приложение в middleware в порядке добавления, так что
    # последний добавленный оказывается внешним. CORS добавляется последним:
    # тогда его заголовки есть и на ответах 413 от BodySizeLimitMiddleware,
    # и на preflight-запросах, которые не доходят до роутеров.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Без этого браузерный JS не видит имя файла при экспорте и паузу
        # после 429 на входе: CORS по умолчанию отдаёт только простые заголовки.
        expose_headers=["X-Request-ID", "Content-Disposition", "Retry-After"],
    )
    # Самый внешний слой: подставляет адрес клиента из X-Forwarded-For, но только
    # если запрос пришёл от доверенного прокси. Иначе rate limit и блокировка
    # входа обходились бы подменой заголовка.
    if settings.trusted_proxy_hosts:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_hosts)

    # NotFound-подклассы DomainError регистрируются до общего обработчика DomainError.
    @app.exception_handler(_NOT_FOUND_EXCEPTIONS[0])
    @app.exception_handler(_NOT_FOUND_EXCEPTIONS[1])
    @app.exception_handler(_NOT_FOUND_EXCEPTIONS[2])
    @app.exception_handler(_NOT_FOUND_EXCEPTIONS[3])
    @app.exception_handler(_NOT_FOUND_EXCEPTIONS[4])
    async def not_found_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.warning("Ресурс не найден", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # Общий обработчик DomainError → 400 (после специфичных 404-handlers).
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        logger.warning("Необработанная доменная ошибка", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health", tags=["system"])
    async def health_check() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(my_documents_router, prefix="/api/v1")
    app.include_router(documents_global_router, prefix="/api/v1")
    app.include_router(documents_router, prefix="/api/v1")
    app.include_router(sources_router, prefix="/api/v1")
    app.include_router(source_files_router, prefix="/api/v1")
    app.include_router(source_snapshots_router, prefix="/api/v1")
    app.include_router(analysis_jobs_router, prefix="/api/v1")
    app.include_router(analysis_jobs_bulk_router, prefix="/api/v1")
    app.include_router(suggestions_router, prefix="/api/v1")
    app.include_router(editor_router, prefix="/api/v1")
    app.include_router(flags_router, prefix="/api/v1")
    app.include_router(dashboard_router, prefix="/api/v1")
    app.include_router(sse_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")

    return app


app = create_app()
