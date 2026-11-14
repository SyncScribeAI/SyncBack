"""Адрес внешнего источника и правила, каким адресам доверять.

Источник по ссылке скачивает воркер с сервера, поэтому ссылка — это запрос
от имени сервера в сеть. Домен задаёт правила: только http(s), без учётных
данных в URL, адрес проходит политику SourceUrlPolicy. Разрешение DNS-имён и
само подключение остаются в инфраструктуре, но итоговые адреса проверяются той
же политикой.

Политика по умолчанию пускает только публичные адреса. В закрытом контуре
внутренние порталы разрешаются в приватные адреса, поэтому администратор
задаёт белый список имён и допустимых сетей. Внутренний адрес принимается,
только когда выполнены оба условия: имя в списке и адрес в разрешённой сети.
Так имена сервисов compose остаются недоступными, пока подсеть Docker не
внесена в список сетей.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from app.domain.exceptions import InvalidSourceUrlError

ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_LOCAL_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_MAX_URL_LENGTH = 2048
_DENIED_MESSAGE = (
    "Адрес не входит в список разрешённых: ссылки на локальные и приватные адреса "
    "принимаются только для внутренних порталов из настроек"
)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _unwrap(address: IpAddress) -> IpAddress:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_forbidden_address(address: IpAddress) -> bool:
    """Адрес, к которому сервер не подключается ни при какой настройке.

    Loopback ведёт в сам контейнер, link-local включает metadata облака
    169.254.169.254, multicast и неопределённый адрес не обозначают один узел.
    """
    address = _unwrap(address)
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def is_public_address(address: IpAddress) -> bool:
    """Адрес доступен из публичного интернета, а не из нашей сети.

    Отсекает loopback, приватные диапазоны, link-local (в том числе metadata
    169.254.169.254), multicast, зарезервированные и неопределённые адреса.
    IPv4-адреса, завёрнутые в IPv6 (::ffff:10.0.0.1), проверяются как IPv4.
    """
    address = _unwrap(address)
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    return address.is_global


def _is_local_name(host: str) -> bool:
    return host in _LOCAL_HOSTS or host.endswith(_LOCAL_HOST_SUFFIXES)


@dataclass(frozen=True, slots=True)
class SourceUrlPolicy:
    """Каким хостам и адресам сервер доверяет при загрузке источника.

    allowed_host_suffixes: точные имена ("wiki.corp") и суффиксы с ведущей точкой
    (".corp" подходит для "a.corp", но не для "corp"). allowed_networks: сети, в
    которые могут разрешаться имена из списка. allow_public: пускать ли любые
    публичные адреса; в контуре без выхода в интернет выставляется в False, и тогда
    действует только белый список.
    """

    allowed_host_suffixes: tuple[str, ...] = ()
    allowed_networks: tuple[IpNetwork, ...] = ()
    allow_public: bool = True

    @classmethod
    def from_lists(
        cls, hosts: Iterable[str], networks: Iterable[str], *, allow_public: bool
    ) -> SourceUrlPolicy:
        """Собирает политику из строк настроек; некорректная сеть даёт ValueError."""
        cleaned = tuple(dict.fromkeys(h.strip().rstrip(".").lower() for h in hosts if h.strip()))
        parsed = tuple(ipaddress.ip_network(n.strip(), strict=False) for n in networks if n.strip())
        return cls(
            allowed_host_suffixes=cleaned, allowed_networks=parsed, allow_public=allow_public
        )

    def is_listed(self, host: str) -> bool:
        """Имя совпадает с записью списка точно или оканчивается на суффикс с точкой."""
        for entry in self.allowed_host_suffixes:
            if entry.startswith("."):
                if host.endswith(entry) and len(host) > len(entry):
                    return True
            elif host == entry:
                return True
        return False

    def allows_host(self, host: str) -> bool:
        """Имя можно принять до разрешения DNS.

        Имя из списка проходит даже с окончанием .local или .internal: так называются
        многие внутренние порталы. Остальные имена проходят, только если разрешены
        публичные адреса и имя не обозначает локальную сеть.
        """
        if self.is_listed(host):
            return True
        return self.allow_public and not _is_local_name(host)

    def allows_address(self, host: str, address: IpAddress) -> bool:
        """Подключение к address для хоста host допустимо."""
        if is_forbidden_address(address):
            return False
        if self.allow_public and is_public_address(address):
            return True
        if not self.is_listed(host):
            return False
        plain = _unwrap(address)
        return any(plain in network for network in self.allowed_networks)


DEFAULT_SOURCE_URL_POLICY = SourceUrlPolicy()


def _parse_ip_literal(host: str) -> IpAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SourceUrl:
    """Проверенный адрес источника: http(s), с хостом, без учётных данных."""

    value: str
    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, raw: str, policy: SourceUrlPolicy = DEFAULT_SOURCE_URL_POLICY) -> SourceUrl:
        """Разбирает и проверяет адрес; при нарушении правил даёт InvalidSourceUrlError."""
        value = raw.strip()
        if not value or len(value) > _MAX_URL_LENGTH:
            raise InvalidSourceUrlError("Ссылка пустая или слишком длинная")

        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError as exc:
            raise InvalidSourceUrlError("Ссылка имеет некорректный формат") from exc

        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise InvalidSourceUrlError("Поддерживаются только ссылки http и https")
        if parts.username is not None or parts.password is not None:
            raise InvalidSourceUrlError("Ссылка не должна содержать учётные данные")

        host = cls._checked_host(parts, policy)
        return cls(value=value, scheme=scheme, host=host, port=port or _DEFAULT_PORTS[scheme])

    @staticmethod
    def _checked_host(parts: SplitResult, policy: SourceUrlPolicy) -> str:
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            raise InvalidSourceUrlError("В ссылке не указан хост")

        literal = _parse_ip_literal(host)
        if literal is not None:
            if not policy.allows_address(host, literal):
                raise InvalidSourceUrlError(_DENIED_MESSAGE)
            return host

        if not policy.allows_host(host):
            raise InvalidSourceUrlError(_DENIED_MESSAGE)
        return host

    @property
    def ip_literal(self) -> IpAddress | None:
        """IP-адрес, если хост задан числом, иначе None (нужно разрешить имя)."""
        return _parse_ip_literal(self.host)

    @property
    def uses_default_port(self) -> bool:
        return self.port == _DEFAULT_PORTS[self.scheme]

    @property
    def host_header(self) -> str:
        """Значение заголовка Host: порт указывается, только если он нестандартный."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.uses_default_port else f"{host}:{self.port}"
