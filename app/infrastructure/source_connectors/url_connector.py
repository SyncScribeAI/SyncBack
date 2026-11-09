"""UrlConnector — скачивает текст источника по HTTP(S)-ссылке.

Защита от SSRF: ссылка проходит правила домена (SourceUrl), имя хоста
разрешается здесь, и все полученные адреса проверяются is_public_address.
Запрос уходит на проверенный IP, а не на имя — иначе между проверкой и
подключением DNS мог бы вернуть другой адрес (DNS rebinding). Заголовок Host
и SNI при этом остаются исходными, поэтому виртуальные хосты и проверка
TLS-сертификата работают как обычно. Редиректы не следуют автоматически:
каждый Location проходит ту же проверку.

Ответ читается потоком и обрывается ошибкой при превышении лимита, а не
буферизуется целиком.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.domain.exceptions import InvalidSourceUrlError, SourceFetchError
from app.domain.interfaces.source_connector import SourceKind, SourceMetadata, SourceRef
from app.domain.source_text import paragraphize, serialize_source
from app.domain.source_url import IpAddress, SourceUrl, is_public_address
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

Resolver = Callable[[str, int], Awaitable[list[IpAddress]]]


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
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver
        self._transport = transport

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

        url = SourceUrl.parse(source.url)
        async with httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            transport=self._transport,
        ) as client:
            await self._check_robots(client, url)
            return await self._download(client, url)

    async def get_metadata(self, source: SourceRef) -> SourceMetadata:
        return SourceMetadata(
            name=source.name,
            type=source.type,
            uploaded_at=source.uploaded_at.isoformat(),
        )

    async def _download(self, client: httpx.AsyncClient, url: SourceUrl) -> tuple[bytes, str]:
        for _ in range(self._max_redirects + 1):
            address = await self._pick_address(url)
            request = self._pinned_request(url, address)
            async with client.stream(
                "GET", request.url, headers=request.headers, extensions=request.extensions
            ) as response:
                if response.is_redirect:
                    url = SourceUrl.parse(urljoin(url.value, response.headers["location"]))
                    continue
                response.raise_for_status()
                raw = await self._read_limited(response, url.value)
                return raw, response.headers.get("content-type", "")
        raise SourceFetchError(f"Слишком много редиректов (больше {self._max_redirects})")

    async def _pick_address(self, url: SourceUrl) -> IpAddress:
        """Возвращает адрес для подключения, убедившись, что все кандидаты публичные."""
        literal = url.ip_literal
        if literal is not None:
            return literal
        resolve = self._resolver
        addresses = await resolve(url.host, url.port)
        if not addresses:
            raise SourceFetchError(f"Имя хоста {url.host} не разрешилось ни в один адрес")
        for address in addresses:
            if not is_public_address(address):
                raise InvalidSourceUrlError(
                    f"Хост {url.host} указывает на локальный или приватный адрес"
                )
        return addresses[0]

    @staticmethod
    def _pinned_request(url: SourceUrl, address: IpAddress) -> _PinnedRequest:
        parts = urlsplit(url.value)
        netloc = f"[{address}]" if address.version == 6 else str(address)
        if not url.uses_default_port:
            netloc = f"{netloc}:{url.port}"
        pinned = urlunsplit((url.scheme, netloc, parts.path or "/", parts.query, ""))
        return _PinnedRequest(
            url=pinned,
            headers={"Host": url.host_header},
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
        """Читает robots.txt тем же защищённым способом; запрет пока только логируется."""
        robots = SourceUrl.parse(f"{url.scheme}://{url.host_header}/robots.txt")
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
        if not parser.can_fetch(_USER_AGENT, url.value):
            logger.warning(
                "robots.txt запрещает скачивание (пока только предупреждаем)",
                extra={"url": url.value},
            )

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
