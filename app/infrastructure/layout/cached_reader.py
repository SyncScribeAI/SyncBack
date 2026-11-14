"""Разбор документа с кэшем PDF рядом с файлом в объектном хранилище."""

import asyncio
import hashlib
import logging
from collections.abc import Sequence

from app.domain.document_geometry import DocumentLayout, PageRect
from app.domain.interfaces.document_parser import IDocumentParserRegistry
from app.domain.interfaces.file_storage import FileStorage, UploadContent
from app.domain.storage_keys import document_layout_key
from app.infrastructure.layout.codec import (
    LayoutCacheMismatch,
    LayoutStamp,
    decode_layout,
    encode_layout,
)

logger = logging.getLogger(__name__)

_CACHE_CONTENT_TYPE = "application/gzip"


class CachedDocumentLayoutReader:
    """Отдаёт разбор документа; PDF разбирается один раз на версию парсера и файл.

    Разбор и поиск прямоугольников идут в потоке: PDFium синхронный и держит общий
    замок, а цикл событий в это время должен обслуживать другие запросы. Отказ записи
    кэша только пишется в лог: пользователь получает разбор, а следующий запрос
    попробует снова.
    """

    def __init__(self, registry: IDocumentParserRegistry, storage: FileStorage) -> None:
        self._registry = registry
        self._storage = storage

    async def read(self, storage_key: str, raw_bytes: bytes) -> DocumentLayout:
        geometry = self._registry.geometry_for(storage_key)
        if geometry is None:
            return DocumentLayout(self._registry.parse_by_filename(storage_key, raw_bytes))

        stamp = LayoutStamp(geometry.version, hashlib.sha256(raw_bytes).hexdigest())
        cache_key = document_layout_key(storage_key)
        cached = await self._load(cache_key, stamp)
        if cached is not None:
            return cached

        layout = await asyncio.to_thread(geometry.parse_layout, raw_bytes)
        await self._save(cache_key, layout, stamp)
        return layout

    async def locate(
        self,
        storage_key: str,
        raw_bytes: bytes,
        layout: DocumentLayout,
        spans: Sequence[tuple[int, int]],
    ) -> list[tuple[PageRect, ...]] | None:
        geometry = self._registry.geometry_for(storage_key)
        if geometry is None or layout.runs is None:
            return None
        return await asyncio.to_thread(geometry.locate, raw_bytes, layout.runs, spans)

    async def _load(self, cache_key: str, stamp: LayoutStamp) -> DocumentLayout | None:
        try:
            if not await self._storage.exists(cache_key):
                return None
            blob = await self._storage.download(cache_key)
        except Exception:
            logger.warning("Кэш разбора %s недоступен, документ разбирается заново", cache_key)
            return None
        try:
            return await asyncio.to_thread(decode_layout, blob, stamp)
        except LayoutCacheMismatch as exc:
            logger.info("Кэш разбора %s устарел (%s), документ разбирается заново", cache_key, exc)
            return None

    async def _save(self, cache_key: str, layout: DocumentLayout, stamp: LayoutStamp) -> None:
        try:
            blob = await asyncio.to_thread(encode_layout, layout, stamp)
            await self._storage.upload(
                cache_key, UploadContent.from_bytes(blob, _CACHE_CONTENT_TYPE)
            )
        except Exception:
            logger.warning("Не удалось сохранить кэш разбора %s", cache_key, exc_info=True)
