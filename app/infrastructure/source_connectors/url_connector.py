"""UrlConnector — скачивает текст источника по HTTP(S)-ссылке.

Защита от SSRF: ссылка проходит правила домена (SourceUrl), имя хоста
разрешается здесь, и все полученные адреса проверяются политикой
SourceUrlPolicy: по умолчанию допустимы только публичные адреса, для внутренних
порталов нужны одновременно имя из белого списка и адрес из разрешённой сети.
Запрос уходит на проверенный IP, а не на имя — иначе между проверкой и
подключением DNS мог бы вернуть другой адрес (DNS rebinding). Заголовок Host
и SNI при этом остаются исходными, поэтому виртуальные хосты и проверка
TLS-сертификата работают как обычно. Редиректы не следуют автоматически:
каждый Location проходит ту же проверку.

Ответ читается потоком и обрывается ошибкой при превышении лимита, а не
буферизуется целиком.

Заголовки авторизации из настроек привязаны к хосту: они уходят только на
свой хост, после редиректа на другой хост не отправляются и в лог не пишутся.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import ssl
import urllib.robotparser
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import Settings
from app.domain.exceptions import (
    InvalidSourceUrlError,
    SourceFetchError,
    SourceRequiresAuthError,
)
from app.domain.interfaces.source_connector import SourceKind, SourceMetadata, SourceRef
from app.domain.source_text import paragraphize, serialize_source
from app.domain.source_url import (
    DEFAULT_SOURCE_URL_POLICY,
    IpAddress,
    SourceUrl,
    SourceUrlPolicy,
)
from app.domain.text_normalization import normalize_text
from app.infrastructure.source_connectors.html_structure import iter_html_fragments

logger = logging.getLogger("syncscribe.connectors.url")

_MAX_BYTES: Final = 5 * 1024 * 1024
_MAX_REDIRECTS: Final = 5
_USER_AGENT: Final = "SyncScribeBot/1.0 (+https://syncscribe.app/bot)"
_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
_ROBOTS_TIMEOUT: Final = 3.0
_ROBOTS_MAX_BYTES: Final = 512 * 1024
_MARKDOWN_SUFFIXES: Final = (".md", ".markdown")
_AUTH_STATUSES: Final = frozenset({401, 403})
# Поле пароля на странице после редиректа на чужой хост означает форму входа SSO.
_PASSWORD_INPUT: Final = re.compile(rb"<input\b[^>]*\btype\s*=\s*[\"']?password", re.IGNORECASE)

Resolver = Callable[[str, int], Awaitable[list[IpAddress]]]
RobotsMode = Literal["off", "log", "enforce"]
AuthHeaders = Mapping[str, Mapping[str, str]]


def load_auth_headers(path: str) -> dict[str, dict[str, str]]:
    """Читает JSON вида {"хост": {"Заголовок": "значение"}} из секрета compose.

    Пустой путь означает, что авторизация не настроена. Значения заголовков в
    сообщения об ошибках не попадают.
    """
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Файл заголовков авторизации должен содержать объект «хост: заголовки»")
    result: dict[str, dict[str, str]] = {}
    for host, headers in data.items():
        if not isinstance(host, str) or not isinstance(headers, dict):
            raise ValueError("Файл заголовков авторизации: ожидается «хост: {заголовок: значение}»")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError(
                f"Файл заголовков авторизации: значения для {host} должны быть строками"
            )
        result[host.strip().rstrip(".").lower()] = dict(headers)
    return result


@dataclass(frozen=True, slots=True)
class _PinnedRequest:
    """Запрос на конкретный IP с исходными Host и SNI."""

    url: str
    headers: dict[str, str]
    extensions: dict[str, str]


async def resolve_host(host: str, port: int) -> list[IpAddress]:
    """Разрешает имя через системный резолвер, не блокируя event loop."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceFetchError(f"Не удалось разрешить имя хоста {host}") from exc
    addresses: list[IpAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = ip_address(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class UrlConnector:
    def __init__(
        self,
        *,
        max_bytes: int = _MAX_BYTES,
        max_redirects: int = _MAX_REDIRECTS,
        resolver: Resolver = resolve_host,
        transport: httpx.AsyncBaseTransport | None = None,
        policy: SourceUrlPolicy = DEFAULT_SOURCE_URL_POLICY,
        user_agent: str = _USER_AGENT,
        robots: RobotsMode = "log",
        timeout: httpx.Timeout = _TIMEOUT,
        ca_bundle: str = "",
        auth_headers: AuthHeaders | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver
        self._transport = transport
        self._policy = policy
        self._user_agent = user_agent
        self._robots = robots
        self._timeout = timeout
        self._auth_headers = {
            host.lower(): dict(headers) for host, headers in (auth_headers or {}).items()
        }
        # Проверка сертификата включена всегда; свой CA добавляет доверие к
        # внутреннему удостоверяющему центру, а не отключает проверку.
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
        )

    def supports(self, source_type: SourceKind) -> bool:
        return source_type == SourceKind.URL

    async def fetch(self, source: SourceRef) -> str:
        raw, content_type = await self._fetch_raw(source)
        return self._to_text(raw, content_type)

    async def fetch_structured(self, source: SourceRef) -> str:
        raw, content_type = await self._fetch_raw(source)
        return self._to_source_text(raw, content_type, source.url or "")

    async def _fetch_raw(self, source: SourceRef) -> tuple[bytes, str]:
        if not source.url:
            raise ValueError(f"url отсутствует для источника {source.id}")

        url = SourceUrl.parse(source.url, self._policy)
        async with httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": self._user_agent},
            timeout=self._timeout,
            transport=self._transport,
            verify=self._verify,
        ) as client:
            try:
                if self._robots != "off":
                    await self._check_robots(client, url)
                return await self._download(client, url)
            except httpx.HTTPError as exc:
                raise SourceFetchError(
                    f"Не удалось загрузить страницу: {type(exc).__name__}"
                ) from exc

    async def get_metadata(self, source: SourceRef) -> SourceMetadata:
        return SourceMetadata(
            name=source.name,
            type=source.type,
            uploaded_at=source.uploaded_at.isoformat(),
        )

    async def _download(self, client: httpx.AsyncClient, url: SourceUrl) -> tuple[bytes, str]:
        origin_host = url.host
        for _ in range(self._max_redirects + 1):
            address = await self._pick_address(url)
            request = self._pinned_request(url, address, self._auth_headers.get(url.host))
            async with client.stream(
                "GET", request.url, headers=request.headers, extensions=request.extensions
            ) as response:
                if response.is_redirect:
                    url = SourceUrl.parse(
                        urljoin(url.value, response.headers["location"]), self._policy
                    )
                    continue
                if response.status_code in _AUTH_STATUSES:
                    raise SourceRequiresAuthError()
                if response.status_code >= 400:
                    raise SourceFetchError(f"Сервер ответил кодом {response.status_code}")
                raw = await self._read_limited(response, url.value)
                content_type = response.headers.get("content-type", "")
                if url.host != origin_host and self._is_login_page(raw, content_type):
                    raise SourceRequiresAuthError()
                return raw, content_type
        raise SourceFetchError(f"Слишком много редиректов (больше {self._max_redirects})")

    @staticmethod
    def _is_login_page(raw: bytes, content_type: str) -> bool:
        return "html" in content_type.lower() and _PASSWORD_INPUT.search(raw) is not None

    async def _pick_address(self, url: SourceUrl) -> IpAddress:
        """Возвращает адрес для подключения, убедившись, что политика пускает все кандидаты.

        Проверяются все адреса, а не только первый: иначе имя с публичным и приватным
        адресом открыло бы путь во внутреннюю сеть при смене порядка ответа DNS.
        """
        literal = url.ip_literal
        if literal is not None:
            return literal
        resolve = self._resolver
        addresses = await resolve(url.host, url.port)
        if not addresses:
            raise SourceFetchError(f"Имя хоста {url.host} не разрешилось ни в один адрес")
        for address in addresses:
            if not self._policy.allows_address(url.host, address):
                raise InvalidSourceUrlError(
                    f"Хост {url.host} указывает на адрес вне списка разрешённых сетей"
                )
        return addresses[0]

    @staticmethod
    def _pinned_request(
        url: SourceUrl, address: IpAddress, extra_headers: Mapping[str, str] | None = None
    ) -> _PinnedRequest:
        parts = urlsplit(url.value)
        netloc = f"[{address}]" if address.version == 6 else str(address)
        if not url.uses_default_port:
            netloc = f"{netloc}:{url.port}"
        pinned = urlunsplit((url.scheme, netloc, parts.path or "/", parts.query, ""))
        return _PinnedRequest(
            url=pinned,
            headers={**(extra_headers or {}), "Host": url.host_header},
            extensions={"sni_hostname": url.host},
        )

    async def _read_limited(self, response: httpx.Response, url: str) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            raise SourceFetchError(f"Содержимое по ссылке больше лимита {self._max_bytes} байт")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                logger.warning(
                    "Содержимое источника превышает лимит", extra={"url": url, "size": total}
                )
                raise SourceFetchError(f"Содержимое по ссылке больше лимита {self._max_bytes} байт")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _check_robots(self, client: httpx.AsyncClient, url: SourceUrl) -> None:
        """Читает robots.txt тем же защищённым способом.

        В режиме log запрет только пишется в лог, в режиме enforce загрузка
        прерывается. Недоступный robots.txt загрузку не останавливает.
        """
        robots = SourceUrl.parse(f"{url.scheme}://{url.host_header}/robots.txt", self._policy)
        try:
            async with asyncio.timeout(_ROBOTS_TIMEOUT):
                address = await self._pick_address(robots)
                request = self._pinned_request(robots, address)
                response = await client.get(
                    request.url, headers=request.headers, extensions=request.extensions
                )
        except (TimeoutError, httpx.HTTPError, SourceFetchError) as exc:
            logger.debug(
                "robots.txt недоступен, продолжаем", extra={"url": url.value, "exc": str(exc)}
            )
            return
        if response.status_code != 200:
            return

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.text[:_ROBOTS_MAX_BYTES].splitlines())
        if parser.can_fetch(self._user_agent, url.value):
            return
        if self._robots == "enforce":
            raise SourceFetchError("robots.txt сайта запрещает загрузку этой страницы")
        logger.warning("robots.txt запрещает скачивание", extra={"url": url.value})

    @staticmethod
    def _to_source_text(raw: bytes, content_type: str, url: str) -> str:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if "html" in media_type:
            return serialize_source(iter_html_fragments(raw))
        text = normalize_text(raw.decode("utf-8", errors="replace"))
        # Markdown по ссылке (raw-файлы репозиториев) уже в формате источника.
        if media_type == "text/markdown" or urlsplit(url).path.lower().endswith(_MARKDOWN_SUFFIXES):
            return text
        return paragraphize(text)

    @staticmethod
    def _to_text(raw: bytes, content_type: str) -> str:
        if "html" in content_type:
            return UrlConnector._strip_html(raw)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _strip_html(raw: bytes) -> str:
        try:
            from bs4 import BeautifulSoup  # noqa: PLC0415
        except ImportError:
            return raw.decode("utf-8", errors="replace")

        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "head"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)


def build_url_connector(settings: Settings) -> UrlConnector:
    """Коннектор с политикой, TLS и заголовками из настроек SOURCE_URL_*."""
    return UrlConnector(
        max_bytes=settings.source_url_max_bytes,
        policy=settings.source_url_policy,
        user_agent=settings.source_url_user_agent,
        robots=settings.source_url_robots,
        timeout=httpx.Timeout(
            connect=settings.source_url_connect_timeout_seconds,
            read=settings.source_url_read_timeout_seconds,
            write=5.0,
            pool=5.0,
        ),
        ca_bundle=settings.source_url_ca_bundle,
        auth_headers=load_auth_headers(settings.source_url_auth_headers_file),
    )
