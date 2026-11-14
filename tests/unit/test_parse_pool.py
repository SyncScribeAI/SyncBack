"""Разбор файлов идёт вне потока событий и не больше заданного числа одновременно."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.domain.interfaces.source_connector import SourceKind, SourceRef
from app.domain.policies import UploadLimits
from app.domain.services.source_service import SourceService
from app.infrastructure.parsers.parse_pool import ThreadParsePool
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.manual_upload_connector import ManualUploadConnector

pytestmark = pytest.mark.asyncio

_PARSE_SECONDS = 0.3


class SlowRegistry(DocumentParserRegistry):
    """Разбор, который держит поток, как большой DOCX или PDF."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []

    def _slow(self) -> None:
        self.threads.append(threading.get_ident())
        time.sleep(_PARSE_SECONDS)

    def extract_text(self, filename: str, raw_bytes: bytes) -> str:
        self._slow()
        return "текст"

    def extract_source_text(self, filename: str, raw_bytes: bytes) -> str:
        self._slow()
        return "текст"


class Storage:
    async def download(self, key: str) -> bytes:
        return b"x"


async def _ticks_while(coro) -> tuple[object, int]:
    """Сколько раз цикл событий успел проснуться, пока шёл разбор."""
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        done.set()
        await task
    return result, ticks


async def test_source_preview_leaves_event_loop_free() -> None:
    registry = SlowRegistry()
    service = SourceService(
        uow=None,  # type: ignore[arg-type]
        file_storage=Storage(),  # type: ignore[arg-type]
        upload_limits=UploadLimits.from_megabytes(1),
        parser_registry=registry,
        parse_pool=ThreadParsePool(1),
    )
    source = SimpleNamespace(storage_key="a.docx")

    text, ticks = await _ticks_while(service.get_source_text(source))  # type: ignore[arg-type]

    assert text == "текст"
    assert registry.threads != [threading.get_ident()]
    # Разбор идёт 0.3 с; цикл, свободный от него, просыпается каждые 10 мс.
    assert ticks >= 10


async def test_source_check_leaves_event_loop_free() -> None:
    registry = SlowRegistry()
    connector = ManualUploadConnector(Storage(), registry, ThreadParsePool(1))  # type: ignore[arg-type]
    ref = SourceRef(
        id=uuid.uuid4(),
        name="a.docx",
        type=SourceKind.FILE,
        storage_key="a.docx",
        url=None,
        uploaded_at=datetime.now(UTC),
    )

    text, ticks = await _ticks_while(connector.fetch_structured(ref))

    assert text == "текст"
    assert ticks >= 10


async def test_pool_limits_concurrent_parsing() -> None:
    pool = ThreadParsePool(2)
    lock = threading.Lock()
    running = 0
    peak = 0

    def parse() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    await asyncio.gather(*(pool.run(parse) for _ in range(8)))

    assert peak == 2
