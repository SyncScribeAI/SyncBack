"""
DI-фабрики FastAPI.

Все сервисы получают UoW (или отдельные репозитории для AuthService, у которого
нет собственного UoW-метода). Конкретные репозитории НЕ инстансируются в этом
файле напрямую — их создаёт SqlAlchemyUnitOfWork или фабрика сервиса.

Фабрики создают concrete SQLAlchemy-репозитории,
но передают их сервисам как значения, удовлетворяющие доменным портам.
Типы аннотаций в фабриках оставлены конкретными — FastAPI DI не понимает
Protocol для Depends, зато mypy/pyright проверят, что concrete-репозитории
действительно реализуют порты через @runtime_checkable.

JWTHandler создаётся один раз на процесс (_get_jwt_handler).

Дашборд:
  DashboardRepository использует отдельную сессию от UoW.
  Это означает READ COMMITTED по умолчанию — uncommitted изменения
  write-транзакций НЕ будут видны dashboard-запросам. Это приемлемо
  для read-model (eventual consistency), но не подходит для операций,
  требующих видеть uncommitted данные текущей транзакции.
  Если потребуется консистентность внутри транзакции — передать
  ту же сессию через параметр или поднять isolation level.
"""

from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.interfaces.analysis_queue import AnalysisQueue
from app.domain.interfaces.dashboard_query_service import IDashboardQueryService
from app.domain.interfaces.document_layout import DocumentLayoutReader
from app.domain.interfaces.event_publisher import IEventPublisher
from app.domain.interfaces.llm_client import LLMClient
from app.domain.interfaces.source_connector import SourceConnector, SourceKind
from app.domain.policies import UploadLimits
from app.domain.services.analysis_flag_service import AnalysisFlagService
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.audit_log_service import AuditLogService
from app.domain.services.auth_service import AuthService
from app.domain.services.dashboard_service import DashboardService
from app.domain.services.document_export_service import DocumentExportService
from app.domain.services.document_service import DocumentService
from app.domain.services.project_service import ProjectService
from app.domain.services.source_service import SourceService
from app.domain.services.source_snapshot_service import SourceSnapshotService
from app.domain.services.suggestion_service import SuggestionService
from app.infrastructure.cache.redis_client import get_redis_client
from app.infrastructure.db.repositories.dashboard_repository import DashboardRepository
from app.infrastructure.db.repositories.project_repository import ProjectRepository
from app.infrastructure.db.repositories.user_repository import UserRepository
from app.infrastructure.db.session import get_db_session
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.events.publishers import SSEEventPublisher
from app.infrastructure.events.sse_broker import get_sse_broker
from app.infrastructure.exporters.exporter_registry import DocumentExporterRegistry
from app.infrastructure.layout.cached_reader import CachedDocumentLayoutReader
from app.infrastructure.llm.factory import get_llm_client
from app.infrastructure.ml_pipeline.factory import pipeline_health_check
from app.infrastructure.parsers.parse_pool import get_parse_pool
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.parsers.upload_file_inspector import ParserUploadFileInspector
from app.infrastructure.queue.celery_analysis_queue import CeleryAnalysisQueue
from app.infrastructure.security.jwt_handler import JWTHandler
from app.infrastructure.security.login_rate_limiter import LoginRateLimiter
from app.infrastructure.security.password_hasher import PasswordHasher
from app.infrastructure.security.refresh_token_store import RefreshTokenStore
from app.infrastructure.source_connectors.manual_upload_connector import ManualUploadConnector
from app.infrastructure.source_connectors.url_connector import UrlConnector, build_url_connector
from app.infrastructure.storage.s3_storage import S3FileStorage

# ---------------------------------------------------------------------------
# Singleton-like infrastructure (one instance per process)
# ---------------------------------------------------------------------------


@lru_cache
def _get_file_storage() -> S3FileStorage:
    return S3FileStorage(get_settings())


@lru_cache
def _get_parser_registry() -> DocumentParserRegistry:
    return DocumentParserRegistry(pdf_max_pages=get_settings().pdf_max_pages)


@lru_cache
def _get_url_connector() -> UrlConnector:
    return build_url_connector(get_settings())


def _connector_for(kind: SourceKind) -> SourceConnector:
    if kind is SourceKind.URL:
        return _get_url_connector()
    return ManualUploadConnector(_get_file_storage(), _get_parser_registry(), get_parse_pool())


@lru_cache
def _get_exporter_registry() -> DocumentExporterRegistry:
    return DocumentExporterRegistry(pdf_annotation_author=get_settings().pdf_annotation_author)


@lru_cache
def _get_analysis_queue() -> CeleryAnalysisQueue:
    return CeleryAnalysisQueue()


@lru_cache
def _get_jwt_handler() -> JWTHandler:
    return JWTHandler(get_settings())


# ---------------------------------------------------------------------------
# Unit of Work (per-request)
# ---------------------------------------------------------------------------


def get_uow(
    session: AsyncSession = Depends(get_db_session),
) -> SqlAlchemyUnitOfWork:
    """Пер-request Unit of Work.

    Один экземпляр на HTTP-запрос — все операции одного запроса
    видят одно и то же состояние сессии и фиксируются одним commit().
    """
    return SqlAlchemyUnitOfWork(session)


# ---------------------------------------------------------------------------
# Services — все используют UoW
# ---------------------------------------------------------------------------


def get_event_publisher() -> IEventPublisher:
    return SSEEventPublisher(get_sse_broker())


def get_suggestion_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
    events: IEventPublisher = Depends(get_event_publisher),
) -> SuggestionService:
    return SuggestionService(uow, events=events)


def get_analysis_job_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
    events: IEventPublisher = Depends(get_event_publisher),
) -> AnalysisJobService:
    return AnalysisJobService(uow, queue=_get_analysis_queue(), events=events)


def get_analysis_flag_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
) -> AnalysisFlagService:
    return AnalysisFlagService(uow)


def get_audit_log_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
) -> AuditLogService:
    return AuditLogService(uow)


def get_project_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
) -> ProjectService:
    return ProjectService(
        uow=uow,
        file_storage=_get_file_storage(),
    )


def get_document_layout_reader() -> DocumentLayoutReader:
    return CachedDocumentLayoutReader(_get_parser_registry(), _get_file_storage(), get_parse_pool())


def get_document_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings),
) -> DocumentService:
    return DocumentService(
        uow=uow,
        file_storage=_get_file_storage(),
        parser_registry=_get_parser_registry(),
        upload_limits=UploadLimits.from_megabytes(settings.max_upload_size_mb),
        download_url_ttl_seconds=settings.s3_presigned_url_expire_seconds,
        file_inspector=ParserUploadFileInspector(settings.pdf_max_pages),
        layout_reader=get_document_layout_reader(),
        parse_pool=get_parse_pool(),
    )


def get_source_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings),
) -> SourceService:
    return SourceService(
        uow=uow,
        file_storage=_get_file_storage(),
        upload_limits=UploadLimits.from_megabytes(settings.max_upload_size_mb),
        parser_registry=_get_parser_registry(),
        url_policy=settings.source_url_policy,
        connector_for=_connector_for,
        max_source_chars=settings.analysis_max_source_chars,
        file_inspector=ParserUploadFileInspector(settings.pdf_max_pages),
        parse_pool=get_parse_pool(),
    )


def get_source_snapshot_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
) -> SourceSnapshotService:
    return SourceSnapshotService(uow=uow, file_storage=_get_file_storage())


def get_document_export_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
) -> DocumentExportService:
    return DocumentExportService(
        uow=uow,
        file_storage=_get_file_storage(),
        exporter_registry=_get_exporter_registry(),
    )


# ---------------------------------------------------------------------------
# Dashboard — read-model инжектируется напрямую, минуя UoW
#
# DashboardRepository получает отдельную сессию (не ту, что UoW).
# Это означает READ COMMITTED isolation — uncommitted изменения
# параллельных write-транзакций НЕ видны. Для read-model это нормально
# (eventual consistency). Если нужна видимость uncommitted данных
# текущего запроса — передать session из get_uow явно.
# ---------------------------------------------------------------------------


def get_dashboard_query_service(
    session: AsyncSession = Depends(get_db_session),
) -> IDashboardQueryService:
    """DashboardRepository как реализация IDashboardQueryService.

    Инжектируется напрямую (без UoW) — read-model не участвует в транзакциях агрегатов.
    Отдельная сессия от UoW — SELECT-запросы дашборда не мешают write-транзакциям.
    """
    return DashboardRepository(session)


def get_dashboard_service(
    dashboard_qs: IDashboardQueryService = Depends(get_dashboard_query_service),
) -> DashboardService:
    """DashboardService поверх read-model дашборда, без UoW."""
    return DashboardService(dashboard_qs=dashboard_qs)


# ---------------------------------------------------------------------------
# Per-request repositories (используются в deps.py для auth/authz)
# ---------------------------------------------------------------------------


def get_user_repository(
    session: AsyncSession = Depends(get_db_session),
) -> UserRepository:
    """Отдельная сессия для UserRepository — Auth-слой вне UoW по дизайну."""
    return UserRepository(session)


def get_project_repository(
    session: AsyncSession = Depends(get_db_session),
) -> ProjectRepository:
    """ProjectRepository для get_allowed_project в api/deps.py."""
    return ProjectRepository(session)


# ---------------------------------------------------------------------------
# Auth (UserRepository живёт вне UoW — отдельная сессия по дизайну)
# ---------------------------------------------------------------------------


def get_login_rate_limiter() -> LoginRateLimiter:
    return LoginRateLimiter(get_redis_client(), get_settings())


def get_refresh_token_store() -> RefreshTokenStore:
    """Хранилище активных refresh-токенов."""
    return RefreshTokenStore(get_redis_client())


async def get_auth_service(
    uow: SqlAlchemyUnitOfWork = Depends(get_uow),
    rate_limiter: LoginRateLimiter = Depends(get_login_rate_limiter),
    refresh_store: RefreshTokenStore = Depends(get_refresh_token_store),
) -> AuthService:
    return AuthService(
        uow,
        PasswordHasher(),
        _get_jwt_handler(),
        rate_limiter,
        refresh_store,
    )


def get_jwt_handler() -> JWTHandler:
    return _get_jwt_handler()


def get_llm_client_instance(
    settings: Settings = Depends(get_settings),
) -> LLMClient:
    """Фабрика LLMClient для FastAPI DI.

    Возвращаемый тип — LLMClient (Protocol), что позволяет mypy/pyright
    проверять совместимость конкретной реализации с доменным контрактом.
    """
    return get_llm_client(settings)


def get_analysis_queue_instance() -> AnalysisQueue:
    return _get_analysis_queue()


def get_pipeline_health_check(
    settings: Settings = Depends(get_settings),
) -> Callable[[], Awaitable[bool | None]]:
    """Проверка конвейера анализа по ANALYSIS_PROVIDER; None из неё — «проверить нечем»."""
    return pipeline_health_check(settings)
