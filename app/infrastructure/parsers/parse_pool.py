"""Ограниченный пул потоков для разбора файлов.

Пул общий на процесс и не привязан к циклу событий: воркер запускает каждую задачу
через asyncio.run, и семафор одного цикла в следующем был бы недействителен.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from app.core.config import get_settings
from app.domain.interfaces.parse_pool import ParsePool

T = TypeVar("T")


class ThreadParsePool(ParsePool):
    def __init__(self, workers: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="parse")

    async def run(self, fn: Callable[..., T], /, *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))


@functools.cache
def get_parse_pool() -> ThreadParsePool:
    return ThreadParsePool(get_settings().parse_pool_workers)
