"""ML-конвейер как HTTP-сервис: POST {endpoint} с JSON запроса 2.0.

Сервис отвечает либо сразу (200 + ответ контракта), либо 202 {request_id} — тогда ответ
забирается опросом GET {endpoint}/{request_id}, пока статус queued/running. 202 принимается
в любом режиме; режим определяет только таймаут одного HTTP-запроса: sync держит
соединение до общего дедлайна, async — короткие запросы и опрос.

Общий дедлайн wait_seconds покрывает всё: POST, повторы и опрос. Повторяются только
временные сбои (сеть, 429, 5xx); 4xx — ошибка запроса, повтор её не исправит.
Тела запросов и ответов в логи и исключения не попадают.
"""

import asyncio
import logging
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.exceptions import LLMInvalidResponseError, LLMTimeoutError
from app.domain.interfaces.document_analyzer import AnalysisRequest, AnalysisResult
from app.infrastructure.llm.http_retry_mixin import backoff_delay
from app.infrastructure.ml_pipeline.schemas import decode_response, encode_request

logger = logging.getLogger("syncscribe.ml_pipeline.http")

_ASYNC_REQUEST_TIMEOUT_SECONDS = 30.0
_PENDING_STATUSES = frozenset({"queued", "running"})
_sleep = asyncio.sleep


class _Accepted(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_id: str | None = None


class _PollStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = None


class HttpAnalyzer:
    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str = "",
        mode: Literal["sync", "async"] = "sync",
        poll_seconds: float = 5.0,
        wait_seconds: float = 1500.0,
        max_retries: int = 3,
        health_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._poll_seconds = poll_seconds
        self._wait_seconds = wait_seconds
        self._max_retries = max_retries
        self._health_url = health_url
        self._transport = transport
        self._request_timeout = wait_seconds if mode == "sync" else _ASYNC_REQUEST_TIMEOUT_SECONDS

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return httpx.AsyncClient(
            headers=headers, timeout=self._request_timeout, transport=self._transport
        )

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        body = encode_request(request)
        try:
            async with asyncio.timeout(self._wait_seconds), self._client() as client:
                response = await self._send(
                    client,
                    "POST",
                    self._endpoint,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Request-ID": request.request_id,
                    },
                )
                del body
                if response.status_code == 202:
                    response = await self._poll(client, _accepted_id(response, request))
                return decode_response(
                    response.content,
                    expected_request_id=request.request_id,
                    source_names=[source.name for source in request.sources],
                )
        except TimeoutError:
            raise LLMTimeoutError(f"ML-сервис не ответил за {self._wait_seconds:g} сек.") from None

    async def _poll(self, client: httpx.AsyncClient, request_id: str) -> httpx.Response:
        # request_id содержит «:» (job_id:attempt) — кодируем как один сегмент пути.
        url = f"{self._endpoint}/{quote(request_id, safe='')}"
        while True:
            await _sleep(self._poll_seconds)
            response = await self._send(client, "GET", url)
            if response.status_code == 202:
                continue
            try:
                status = _PollStatus.model_validate_json(response.content).status
            except ValidationError:
                raise LLMInvalidResponseError("Ответ опроса ML-сервиса не JSON") from None
            if status not in _PENDING_STATUSES:
                return response

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            retry_after: str | None = None
            try:
                response = await client.request(method, url, content=content, headers=headers)
            except httpx.TimeoutException:
                failure = "ML-сервис не ответил вовремя"
            except httpx.TransportError:
                failure = "Не удалось подключиться к ML-сервису"
            else:
                if response.is_success:
                    return response
                if response.status_code != 429 and response.status_code < 500:
                    raise LLMInvalidResponseError(
                        f"ML-сервис вернул HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                failure = f"ML-сервис вернул HTTP {response.status_code}"
                retry_after = response.headers.get("retry-after")
            if attempt == self._max_retries:
                raise LLMTimeoutError(f"{failure} (попыток: {attempt + 1})")
            logger.info("%s, повтор %d из %d", failure, attempt + 1, self._max_retries)
            await _sleep(backoff_delay(attempt, retry_after))
        raise AssertionError("unreachable")

    async def health_check(self) -> bool | None:
        if not self._health_url:
            # Без отдельного адреса проверки здоровья доступность видна только по анализу.
            return None
        try:
            async with self._client() as client:
                response = await client.get(self._health_url, timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.is_success


def _accepted_id(response: httpx.Response, request: AnalysisRequest) -> str:
    """Сервис может выдать свой id задачи; если нет — опрашиваем по нашему request_id."""
    try:
        accepted = _Accepted.model_validate_json(response.content)
    except ValidationError:
        return request.request_id
    return accepted.request_id or request.request_id
