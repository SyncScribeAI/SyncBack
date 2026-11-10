"""Общие зависимости задач анализа: хранилище, парсеры, коннекторы источников, события.

Объекты создаются лениво и один раз на процесс воркера (prefork): клиенты без привязки к
event loop переживают asyncio.run() каждой задачи. Клиенты, привязанные к loop (Redis),
сюда не входят — их создаёт сама задача.
"""

from __future__ import annotations

import functools

from app.core.config import get_settings
from app.domain.interfaces.event_publisher import IEventPublisher
from app.domain.interfaces.source_connector import SourceConnector, SourceKind, SourceRef
from app.infrastructure.db.models.source import Source
from app.infrastructure.events.publishers import RedisEventPublisher
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.manual_upload_connector import ManualUploadConnector
from app.infrastructure.source_connectors.url_connector import UrlConnector
from app.infrastructure.storage.s3_storage import S3FileStorage


@functools.cache
def get_storage() -> S3FileStorage:
    return S3FileStorage()


@functools.cache
def get_parser_registry() -> DocumentParserRegistry:
    return DocumentParserRegistry()


@functools.cache
def _get_manual_connector() -> ManualUploadConnector:
    return ManualUploadConnector(get_storage(), get_parser_registry())


@functools.cache
def _get_url_connector() -> UrlConnector:
    return UrlConnector()


def get_connector_for(source_kind: SourceKind) -> SourceConnector:
    if source_kind == SourceKind.FILE:
        return _get_manual_connector()
    if source_kind == SourceKind.URL:
        return _get_url_connector()
    raise ValueError(f"Неизвестный SourceKind: {source_kind}")


def event_publisher() -> IEventPublisher:
    settings = get_settings()
    return RedisEventPublisher(settings.redis_url, settings.redis_sse_channel)


def build_source_ref(source: Source) -> SourceRef:
    kind_map = {"file": SourceKind.FILE, "url": SourceKind.URL}
    source_kind = kind_map.get(source.type.value)
    if source_kind is None:
        raise ValueError(f"Неизвестный тип источника в БД: {source.type.value}")
    return SourceRef(
        id=source.id,
        name=source.name,
        type=source_kind,
        storage_key=source.storage_key,
        url=source.url,
        uploaded_at=source.created_at,
    )
