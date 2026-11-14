"""Правила домена для ссылок на источники."""

import ipaddress

import pytest

from app.domain.exceptions import InvalidSourceUrlError
from app.domain.source_url import SourceUrl, SourceUrlPolicy, is_public_address


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/spec",
        "http://example.com:8080/a?b=c",
        "HTTPS://Docs.Example.COM/page",
        "http://93.184.216.34/",
        "http://[2606:2800:220:1:248:1893:25c8:1946]/",
    ],
)
def test_accepts_public_http_urls(raw: str) -> None:
    assert SourceUrl.parse(raw).value == raw


def test_parse_normalises_scheme_host_and_port() -> None:
    url = SourceUrl.parse("HTTPS://Docs.Example.COM./page")
    assert (url.scheme, url.host, url.port) == ("https", "docs.example.com", 443)
    assert url.host_header == "docs.example.com"
    assert url.uses_default_port

    url = SourceUrl.parse("http://example.com:8080/")
    assert url.host_header == "example.com:8080"
    assert not url.uses_default_port

    url = SourceUrl.parse("http://[2606:2800:220:1:248:1893:25c8:1946]:81/")
    assert url.host_header == "[2606:2800:220:1:248:1893:25c8:1946]:81"
    assert url.ip_literal == ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com",
        "example.com/no-scheme",
        "http://",
        "http://user:secret@example.com/",
        "http://user@example.com/",
        "http://example.com:99999/",
        "http://localhost/",
        "http://LOCALHOST:8000/",
        "http://api.svc.local/",
        "http://db.internal/",
        "http://printer.home.arpa/",
        "http://127.0.0.1/",
        "http://127.1.2.3/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_rejects_unsafe_urls(raw: str) -> None:
    with pytest.raises(InvalidSourceUrlError):
        SourceUrl.parse(raw)


def test_rejects_too_long_url() -> None:
    with pytest.raises(InvalidSourceUrlError):
        SourceUrl.parse("https://example.com/" + "a" * 2048)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.127.255.255", False),
        ("224.0.0.251", False),
        ("240.0.0.1", False),
        ("0.0.0.0", False),
        ("::", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("ff02::1", False),
        ("::ffff:192.168.0.1", False),
        ("::ffff:8.8.8.8", True),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public


def _policy(**kwargs: object) -> SourceUrlPolicy:

    defaults: dict[str, object] = {
        "hosts": ["wiki.corp", ".portal.local"],
        "networks": ["10.0.0.0/8"],
        "allow_public": True,
    }
    defaults.update(kwargs)
    return SourceUrlPolicy.from_lists(
        defaults["hosts"], defaults["networks"], allow_public=bool(defaults["allow_public"])
    )


def test_policy_suffix_needs_leading_dot_match() -> None:
    policy = _policy()
    assert policy.is_listed("docs.portal.local")
    assert not policy.is_listed("portal.local")
    assert not policy.is_listed("xwiki.corp")
    assert policy.is_listed("wiki.corp")


def test_listed_local_name_passes_parse() -> None:
    assert SourceUrl.parse("https://docs.portal.local/a", _policy()).host == "docs.portal.local"
    with pytest.raises(InvalidSourceUrlError):
        SourceUrl.parse("https://other.local/a", _policy())


def test_private_literal_needs_listing_and_network() -> None:
    with pytest.raises(InvalidSourceUrlError):
        SourceUrl.parse("http://10.1.2.3/", _policy())
    policy = _policy(hosts=["10.1.2.3"])
    assert SourceUrl.parse("http://10.1.2.3/", policy).host == "10.1.2.3"


def test_metadata_address_is_forbidden_with_any_policy() -> None:
    policy = _policy(hosts=["169.254.169.254"], networks=["0.0.0.0/0"])
    with pytest.raises(InvalidSourceUrlError):
        SourceUrl.parse("http://169.254.169.254/latest", policy)


def test_public_disabled_keeps_only_whitelist() -> None:
    policy = _policy(allow_public=False)
    with pytest.raises(InvalidSourceUrlError):
        SourceUrl.parse("https://example.com/", policy)
    assert SourceUrl.parse("https://wiki.corp/", policy).host == "wiki.corp"


def test_mapped_ipv6_is_checked_as_ipv4() -> None:
    policy = _policy()
    address = ipaddress.ip_address("::ffff:10.0.0.5")
    assert policy.allows_address("wiki.corp", address)
    assert not policy.allows_address("evil.example.com", address)
