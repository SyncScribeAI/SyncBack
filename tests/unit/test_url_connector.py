"""UrlConnector: подключение к проверенному адресу, редиректы, лимит размера."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.domain.exceptions import (
    InvalidSourceUrlError,
    SourceFetchError,
    SourceRequiresAuthError,
)
from app.domain.interfaces.source_connector import SourceKind, SourceRef
from app.domain.source_url import IpAddress, SourceUrlPolicy
from app.infrastructure.source_connectors.url_connector import (
    UrlConnector,
    load_auth_headers,
    resolve_host,
)

PUBLIC_IP = ipaddress.ip_address("93.184.216.34")
PRIVATE_IP = ipaddress.ip_address("10.0.0.5")


def _ref(url: str) -> SourceRef:
    return SourceRef(
        id=uuid.uuid4(),
        name="spec",
        type=SourceKind.URL,
        storage_key=None,
        url=url,
        uploaded_at=datetime.now(UTC),
    )


def _resolver(table: dict[str, list[IpAddress]]):
    async def resolve(host: str, port: int) -> list[IpAddress]:
        return table.get(host, [])

    return resolve


def _connector(handler, table, **kwargs) -> UrlConnector:
    return UrlConnector(resolver=_resolver(table), transport=httpx.MockTransport(handler), **kwargs)


async def test_connects_to_resolved_ip_and_keeps_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})

    connector = _connector(handler, {"example.com": [PUBLIC_IP]})

    text = await connector.fetch(_ref("https://example.com/spec?v=1"))

    assert text == "hello"
    page = seen[-1]
    assert page.url.host == str(PUBLIC_IP)
    assert page.url.path == "/spec"
    assert page.url.query == b"v=1"
    assert page.headers["host"] == "example.com"
    assert page.extensions["sni_hostname"] == "example.com"
    assert seen[0].url.path == "/robots.txt"


async def test_rejects_host_resolving_to_private_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запрос не должен уходить в сеть")

    connector = _connector(handler, {"internal.example.com": [PUBLIC_IP, PRIVATE_IP]})

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("http://internal.example.com/"))


async def test_unresolvable_host_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запрос не должен уходить в сеть")

    connector = _connector(handler, {})

    with pytest.raises(SourceFetchError):
        await connector.fetch(_ref("http://nowhere.example.com/"))


async def test_redirect_to_private_host_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    connector = _connector(handler, {"example.com": [PUBLIC_IP]})

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("http://example.com/"))


async def test_redirect_is_followed_and_rechecked() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://cdn.example.net/doc"})
        return httpx.Response(
            200,
            text="<html><body><p>Текст</p></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    connector = _connector(
        handler,
        {"example.com": [PUBLIC_IP], "cdn.example.net": [ipaddress.ip_address("8.8.8.8")]},
    )

    text = await connector.fetch(_ref("http://example.com/"))

    assert text == "Текст"
    assert hosts == ["example.com", "example.com", "cdn.example.net"]


async def test_too_many_redirects_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://example.com/loop"})

    connector = _connector(handler, {"example.com": [PUBLIC_IP]}, max_redirects=2)

    with pytest.raises(SourceFetchError, match="редиректов"):
        await connector.fetch(_ref("http://example.com/"))


async def test_oversized_body_is_rejected_not_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 2048, headers={"content-type": "text/plain"})

    connector = _connector(handler, {"example.com": [PUBLIC_IP]}, max_bytes=1024)

    with pytest.raises(SourceFetchError, match="лимита"):
        await connector.fetch(_ref("http://example.com/big"))


async def test_declared_oversized_content_length_is_rejected_early() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-length": "999999", "content-type": "text/plain"}
        )

    connector = _connector(handler, {"example.com": [PUBLIC_IP]}, max_bytes=1024)

    with pytest.raises(SourceFetchError, match="лимита"):
        await connector.fetch(_ref("http://example.com/big"))


async def test_http_error_becomes_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503)

    connector = _connector(handler, {"example.com": [PUBLIC_IP]})

    with pytest.raises(SourceFetchError, match="503"):
        await connector.fetch(_ref("http://example.com/"))


async def test_robots_failure_does_not_block_fetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("robots down")
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    connector = _connector(handler, {"example.com": [PUBLIC_IP]})

    assert await connector.fetch(_ref("http://example.com/")) == "ok"


async def test_system_resolver_returns_addresses_for_localhost() -> None:
    addresses = await resolve_host("localhost", 80)

    assert addresses
    assert all(address.is_loopback for address in addresses)


async def test_system_resolver_reports_unknown_host() -> None:
    with pytest.raises(SourceFetchError):
        await resolve_host("host.invalid", 80)


# ---------------------------------------------------------------------------
# Внутренние порталы: белый список имён и разрешённые сети
# ---------------------------------------------------------------------------

PORTAL_IP = ipaddress.ip_address("10.20.0.7")
DOCKER_IP = ipaddress.ip_address("172.18.0.3")
INTERNAL = SourceUrlPolicy.from_lists(
    ["wiki.corp", ".portal.corp"], ["10.20.0.0/16"], allow_public=True
)


def _ok_page(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, text="страница", headers={"content-type": "text/plain"})


def _never(request: httpx.Request) -> httpx.Response:
    raise AssertionError("запрос не должен уходить в сеть")


async def test_listed_internal_host_in_allowed_network_passes() -> None:
    table = {"wiki.corp": [PORTAL_IP], "docs.portal.corp": [PORTAL_IP]}
    connector = _connector(_ok_page, table, policy=INTERNAL)

    assert await connector.fetch(_ref("https://wiki.corp/page")) == "страница"
    assert await connector.fetch(_ref("https://docs.portal.corp/page")) == "страница"


async def test_same_address_under_unlisted_name_is_rejected() -> None:
    connector = _connector(_never, {"other.example.com": [PORTAL_IP]}, policy=INTERNAL)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("https://other.example.com/page"))


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "fe80::1"])
async def test_loopback_and_link_local_rejected_even_for_listed_host(address: str) -> None:
    policy = SourceUrlPolicy.from_lists(["wiki.corp"], ["0.0.0.0/0", "::/0"], allow_public=True)
    connector = _connector(_never, {"wiki.corp": [ipaddress.ip_address(address)]}, policy=policy)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("https://wiki.corp/"))


@pytest.mark.parametrize("service", ["ml", "postgres", "redis", "seaweedfs", "backend"])
async def test_compose_service_names_are_rejected(service: str) -> None:
    connector = _connector(_never, {service: [DOCKER_IP]}, policy=INTERNAL)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref(f"http://{service}:8000/"))


async def test_docker_subnet_outside_allowed_networks_blocks_listed_name() -> None:
    policy = SourceUrlPolicy.from_lists(["postgres"], ["10.20.0.0/16"], allow_public=True)
    connector = _connector(_never, {"postgres": [DOCKER_IP]}, policy=policy)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("http://postgres:5432/"))


async def test_redirect_from_listed_host_to_unlisted_host_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://files.example.org/x"})

    table = {"wiki.corp": [PORTAL_IP], "files.example.org": [PORTAL_IP]}
    connector = _connector(handler, table, policy=INTERNAL)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("https://wiki.corp/"))


async def test_auth_header_goes_only_to_its_host() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["host"], request.headers.get("authorization")))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers["host"] == "wiki.corp":
            return httpx.Response(302, headers={"location": "https://cdn.portal.corp/file"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    table = {"wiki.corp": [PORTAL_IP], "cdn.portal.corp": [PORTAL_IP]}
    connector = _connector(
        handler,
        table,
        policy=INTERNAL,
        auth_headers={"wiki.corp": {"Authorization": "Bearer token"}},
    )

    assert await connector.fetch(_ref("https://wiki.corp/page")) == "ok"
    assert ("wiki.corp", "Bearer token") in seen
    assert all(auth is None for host, auth in seen if host == "cdn.portal.corp")


async def test_public_address_rejected_when_public_disabled() -> None:
    policy = SourceUrlPolicy.from_lists(["wiki.corp"], ["10.20.0.0/16"], allow_public=False)
    connector = _connector(_never, {"example.com": [PUBLIC_IP]}, policy=policy)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("https://example.com/"))


async def test_listed_host_with_public_address_needs_allowed_network_when_public_disabled() -> None:
    policy = SourceUrlPolicy.from_lists(["wiki.corp"], ["10.20.0.0/16"], allow_public=False)
    connector = _connector(_never, {"wiki.corp": [PUBLIC_IP]}, policy=policy)

    with pytest.raises(InvalidSourceUrlError):
        await connector.fetch(_ref("https://wiki.corp/"))


@pytest.mark.parametrize("code", [401, 403])
async def test_unauthorized_response_asks_to_upload_file(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(code)

    connector = _connector(handler, {"wiki.corp": [PORTAL_IP]}, policy=INTERNAL)

    with pytest.raises(SourceRequiresAuthError, match="Сохраните её как файл"):
        await connector.fetch_structured(_ref("https://wiki.corp/"))


async def test_redirect_to_login_form_on_other_host_requires_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers["host"] == "wiki.corp":
            return httpx.Response(302, headers={"location": "https://sso.portal.corp/login"})
        return httpx.Response(
            200,
            text='<html><form><input name="p" TYPE="password"></form></html>',
            headers={"content-type": "text/html"},
        )

    table = {"wiki.corp": [PORTAL_IP], "sso.portal.corp": [PORTAL_IP]}
    connector = _connector(handler, table, policy=INTERNAL)

    with pytest.raises(SourceRequiresAuthError):
        await connector.fetch_structured(_ref("https://wiki.corp/"))


async def test_password_field_on_same_host_is_regular_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text='<html><body><p>Поле</p><input type="password"></body></html>',
            headers={"content-type": "text/html"},
        )

    connector = _connector(handler, {"wiki.corp": [PORTAL_IP]}, policy=INTERNAL)

    assert "Поле" in await connector.fetch_structured(_ref("https://wiki.corp/"))


async def test_robots_off_skips_request() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    connector = _connector(handler, {"example.com": [PUBLIC_IP]}, robots="off")

    assert await connector.fetch(_ref("http://example.com/doc")) == "ok"
    assert paths == ["/doc"]


async def test_robots_enforce_blocks_disallowed_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        raise AssertionError("страница не должна загружаться")

    connector = _connector(handler, {"example.com": [PUBLIC_IP]}, robots="enforce")

    with pytest.raises(SourceFetchError, match="robots"):
        await connector.fetch(_ref("http://example.com/doc"))


def test_auth_headers_file_is_read_by_host(tmp_path) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"Wiki.Corp.": {"Authorization": "Bearer t"}}', encoding="utf-8")

    assert load_auth_headers(str(path)) == {"wiki.corp": {"Authorization": "Bearer t"}}
    assert load_auth_headers("") == {}


def test_auth_headers_file_rejects_wrong_shape(tmp_path) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"wiki.corp": "Bearer t"}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_auth_headers(str(path))
