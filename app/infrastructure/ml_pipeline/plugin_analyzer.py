"""ML-конвейер как Python-пакет в образе воркера: callable «dict запроса → dict ответа».

Callable импортируется при создании адаптера, т. е. при старте воркера: опечатка в
ML_PLUGIN_ENTRYPOINT или неустановленный пакет видны сразу, а не на первой задаче.

Конвейер синхронный и долгий, поэтому вызывается в отдельном потоке. Дедлайн ограничивает
ожидание, но не останавливает поток: Python не умеет прервать чужой поток. Застрявший
вызов доработает в фоне, а задачу освободит дедлайн; от накопления таких потоков защищает
soft_time_limit задачи Celery и перезапуск процесса воркера.
"""

import asyncio
import importlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from app.domain.exceptions import LLMInvalidResponseError, LLMTimeoutError
from app.domain.interfaces.document_analyzer import AnalysisRequest, AnalysisResult
from app.infrastructure.ml_pipeline.schemas import decode_response, to_contract

logger = logging.getLogger("syncscribe.ml_pipeline.plugin")

PluginCallable = Callable[[dict[str, Any]], Any]


def load_entrypoint(entrypoint: str) -> PluginCallable:
    module_name, _, attribute = entrypoint.partition(":")
    if not module_name or not attribute:
        raise ValueError(
            f"ML_PLUGIN_ENTRYPOINT ожидается как package.module:function: {entrypoint!r}"
        )
    try:
        target: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"ML-плагин не установлен: модуль {module_name!r} не найден") from exc
    for part in attribute.split("."):
        target = getattr(target, part, None)
        if target is None:
            raise ValueError(f"В модуле {module_name!r} нет {attribute!r}")
    if not callable(target):
        raise ValueError(f"{entrypoint!r} не является функцией")
    plugin: PluginCallable = target
    return plugin


class PluginAnalyzer:
    def __init__(self, plugin: PluginCallable | str, *, wait_seconds: float) -> None:
        self._plugin = load_entrypoint(plugin) if isinstance(plugin, str) else plugin
        self._wait_seconds = wait_seconds

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        payload = to_contract(request)
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._plugin, payload), timeout=self._wait_seconds
            )
        except TimeoutError:
            raise LLMTimeoutError(f"ML-плагин не ответил за {self._wait_seconds:g} сек.") from None
        except Exception as exc:
            # Конвейер сам превращает сбои модели в status=failed; исключение наружу —
            # ошибка плагина или его установки, повтор её не исправит. Текст исключения
            # в лог не пишем: в нём может оказаться фрагмент документа.
            logger.warning(
                "ML-плагин завершился исключением %s (request_id=%s)",
                type(exc).__name__,
                request.request_id,
            )
            raise LLMInvalidResponseError(f"ML-плагин упал: {type(exc).__name__}") from None
        if not isinstance(raw, Mapping | bytes | str):
            raise LLMInvalidResponseError(
                f"ML-плагин вернул {type(raw).__name__}, ожидался dict ответа контракта"
            )
        return decode_response(
            raw,
            expected_request_id=request.request_id,
            source_names=[source.name for source in request.sources],
        )

    async def health_check(self) -> bool:
        # Callable загружен при создании адаптера; модель за ним проверить нечем, не
        # запуская анализ, — это делает сам конвейер.
        return True
