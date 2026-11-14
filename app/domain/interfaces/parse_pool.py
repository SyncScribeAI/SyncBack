"""Порт пула, в котором идёт синхронный разбор файлов.

Разбор DOCX, PDF и HTML занимает CPU на секунды. В потоке событий он остановил бы все
запросы процесса, а asyncio.to_thread без предела пускает столько разборов, сколько
пришло запросов, и вместе с ними держит в памяти их файлы и очередь к замку PDFium.
Поэтому разбор идёт через пул с ограниченным числом потоков.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class ParsePool(Protocol):
    async def run(self, fn: Callable[..., T], /, *args: Any) -> T:
        """Выполнить fn(*args) вне потока событий и вернуть результат."""
        ...


class UnboundedParsePool:
    """Пул по умолчанию для тестов и скриптов: тот же уход из потока событий, без предела.

    Процессы API и воркеров получают ограниченный пул из настроек.
    """

    async def run(self, fn: Callable[..., T], /, *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)


UNBOUNDED_PARSE_POOL = UnboundedParsePool()
