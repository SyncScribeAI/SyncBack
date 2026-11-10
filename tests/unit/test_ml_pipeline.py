"""Подключение ML-конвейера по контракту 2.0: схема, адаптеры, фабрика, настройки.

Фикстуры tests/fixtures/ml_contract собраны по тексту контракта, а не скопированы из
репозитория ML: тесты проверяют наш контракт и не зависят от чужого кода.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.domain.exceptions import LLMInvalidResponseError, LLMTimeoutError
from app.domain.interfaces.document_analyzer import (
    AnalysisErrorCode,
    AnalysisRequest,
    AnalysisStatus,
    SourcePayload,
)
from app.domain.interfaces.source_connector import SourceKind
from app.domain.lifecycle import AnalysisJobLifecycle
from app.domain.services.analysis_request_builder import DocumentInput, build_analysis_request
from app.domain.services.analysis_result_mapper import map_result
from app.domain.value_objects import AnalysisJobStatusVO
from app.infrastructure.ml_pipeline import http_analyzer as http_module
from app.infrastructure.ml_pipeline.factory import (
    analysis_limits,
    get_document_analyzer,
    uses_document_analyzer,
)
from app.infrastructure.ml_pipeline.http_analyzer import HttpAnalyzer
from app.infrastructure.ml_pipeline.plugin_analyzer import PluginAnalyzer, load_entrypoint
from app.infrastructure.ml_pipeline.schemas import decode_response, encode_request, to_contract
from app.infrastructure.ml_pipeline.stub_analyzer import StubAnalyzer
from app.infrastructure.parsers.markdown_parser import MarkdownParser

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ml_contract"
MD = "## Сроки\n\nСрок ответа на обращение — 10 рабочих дней.\n\n## Оплата\n\nОплата принимается картой.\n"
SECRET_TEXT = "Срок ответа на обращение"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _request() -> AnalysisRequest:
    document = DocumentInput(
        id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        name="Регламент.md",
        format="markdown",
        plain_text=MD,
        blocks=MarkdownParser().parse(MD.encode()).blocks,
    )
    source = SourcePayload(
        id="44444444-4444-4444-8444-444444444444",
        name="Решения встречи от 02.11.2026",
        type=SourceKind.FILE,
        text="# Решения\n\nСрок ответа на обращение: 5 рабочих дней.\n\n"
        "- Добавлена оплата по счёту для юридических лиц.",
    )
    return build_analysis_request(
        job_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        attempt=0,
        document=document,
        sources=[source],
    )


# --- схема контракта ---------------------------------------------------------------


def test_request_matches_contract_fixture() -> None:
    assert to_contract(_request()) == _fixture("example_request")


def test_encoded_request_is_single_utf8_buffer_without_escapes() -> None:
    body = encode_request(_request())
    assert isinstance(body, bytes)
    assert SECRET_TEXT.encode() in body
    assert json.loads(body) == _fixture("example_request")


def test_example_response_maps_to_drafts_without_drops() -> None:
    request = _request()
    result = decode_response(
        (FIXTURES / "example_response.json").read_bytes(), expected_request_id=request.request_id
    )
    assert result.status is AnalysisStatus.COMPLETED
    mapped = map_result(result, request.document.blocks)
    assert mapped.dropped == {}
    assert [(d.block_ref, d.change_type.value, d.section_ref) for d in mapped.suggestions] == [
        ("p-1", "modify", "Сроки"),
        ("p-3", "add", "Оплата"),
    ]
    assert mapped.suggestions[1].original_text == "Оплата принимается картой."
    assert [(f.reason, f.block_ref, len(f.quotes)) for f in mapped.flags] == [
        ("sources_conflict", "p-1", 2)
    ]
    assert AnalysisJobLifecycle.outcome_for(result).status is AnalysisJobStatusVO.SUCCESS


def test_failed_response_is_final_invalid_request() -> None:
    result = decode_response(_fixture("failed_response"))
    assert result.status is AnalysisStatus.FAILED
    assert result.error is not None and result.error.code is AnalysisErrorCode.INVALID_REQUEST
    outcome = AnalysisJobLifecycle.outcome_for(result)
    assert (outcome.error_code, outcome.retryable) == ("ML_INVALID_REQUEST", False)


def test_partial_response_keeps_unknown_meta_and_becomes_partial_success() -> None:
    result = decode_response(_fixture("partial_response"))
    assert result.meta["future_key"] == {"x": 1}
    outcome = AnalysisJobLifecycle.outcome_for(result)
    assert (outcome.status, outcome.error_message) == (
        AnalysisJobStatusVO.PARTIAL_SUCCESS,
        "1 блоков не удалось проверить",
    )


def _response(**override: Any) -> dict[str, Any]:
    return {**_fixture("example_response"), **override}


@pytest.mark.parametrize(
    "raw",
    [
        _response(contract_version="1.0"),
        _response(status="done"),
        _response(suggestions=[{"change_type": "modify", "old_text": SECRET_TEXT}]),
        {"status": "completed"},
        b"not json " + SECRET_TEXT.encode(),
    ],
)
def test_response_out_of_contract_is_rejected_without_leaking_text(raw: Any) -> None:
    with pytest.raises(LLMInvalidResponseError) as error:
        decode_response(raw)
    assert SECRET_TEXT not in str(error.value)


def test_unknown_change_type_reaches_mapper_and_drops_one_change() -> None:
    suggestions = _fixture("example_response")["suggestions"]
    raw = _response(suggestions=[{**suggestions[0], "change_type": "rewrite"}, suggestions[1]])
    request = _request()
    mapped = map_result(decode_response(raw), request.document.blocks)
    assert mapped.dropped == {"unknown_change_type": 1}
    assert len(mapped.suggestions) == 1


@pytest.mark.parametrize("error", [None, {"code": "model_overloaded", "message": "x"}])
def test_unknown_or_missing_error_code_is_transient(error: Any) -> None:
    result = decode_response(_response(status="failed", suggestions=[], error=error))
    assert result.error is not None and result.error.code is AnalysisErrorCode.PIPELINE_ERROR
    assert AnalysisJobLifecycle.outcome_for(result).retryable


def test_response_to_other_attempt_is_rejected() -> None:
    with pytest.raises(LLMInvalidResponseError, match="request_id"):
        decode_response(_fixture("example_response"), expected_request_id="other:1")
    decode_response(_response(request_id=None), expected_request_id="other:1")


# --- плагин --------------------------------------------------------------------------


def _echo_plugin(payload: dict[str, Any]) -> dict[str, Any]:
    return _response(request_id=payload["request_id"])


async def test_plugin_receives_contract_dict_and_returns_result() -> None:
    received: list[dict[str, Any]] = []

    def plugin(payload: dict[str, Any]) -> dict[str, Any]:
        received.append(payload)
        return _echo_plugin(payload)

    result = await PluginAnalyzer(plugin, wait_seconds=5).analyze(_request())
    assert received == [_fixture("example_request")]
    assert len(result.suggestions) == 2
    assert await PluginAnalyzer(plugin, wait_seconds=5).health_check()


async def test_plugin_string_response_is_parsed_too() -> None:
    def plugin(payload: dict[str, Any]) -> str:
        return json.dumps(_echo_plugin(payload), ensure_ascii=False)

    assert len((await PluginAnalyzer(plugin, wait_seconds=5).analyze(_request())).suggestions) == 2


async def test_plugin_deadline_frees_the_task() -> None:
    def slow(_: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.5)
        return {}

    started = time.perf_counter()
    with pytest.raises(LLMTimeoutError):
        await PluginAnalyzer(slow, wait_seconds=0.05).analyze(_request())
    assert time.perf_counter() - started < 0.4


async def test_plugin_exception_is_final_and_does_not_leak_text() -> None:
    def broken(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(SECRET_TEXT)

    with pytest.raises(LLMInvalidResponseError) as error:
        await PluginAnalyzer(broken, wait_seconds=5).analyze(_request())
    assert "RuntimeError" in str(error.value) and SECRET_TEXT not in str(error.value)


async def test_plugin_returning_non_mapping_is_invalid() -> None:
    with pytest.raises(LLMInvalidResponseError, match="list"):
        await PluginAnalyzer(lambda _: [], wait_seconds=5).analyze(_request())


def test_entrypoint_is_loaded_at_startup() -> None:
    assert load_entrypoint("json:loads") is json.loads
    assert load_entrypoint("os.path:join.__call__")
    for bad, message in [
        ("json", "package.module:function"),
        ("no_such_ml_package.contract:run", "не установлен"),
        ("json:no_such_function", "нет"),
        ("json:__doc__", "не является функцией"),
    ]:
        with pytest.raises(ValueError, match=message):
            load_entrypoint(bad)


# --- HTTP ------------------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(http_module, "_sleep", fake_sleep)
    return delays


def _http(handler, **options: Any) -> HttpAnalyzer:
    return HttpAnalyzer(
        "http://ml.test/v2/analyze", transport=httpx.MockTransport(handler), **options
    )


def _ok(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(200, json=_response(request_id=payload["request_id"]))


async def test_http_sync_sends_encoded_bytes_with_auth(no_sleep: list[float]) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    result = await _http(handler, api_key="ml-key").analyze(_request())
    assert len(result.suggestions) == 2
    sent = seen[0]
    assert sent.method == "POST" and sent.content == encode_request(_request())
    assert sent.headers["authorization"] == "Bearer ml-key"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["x-request-id"] == _request().request_id


async def test_http_retries_transient_failures(no_sleep: list[float]) -> None:
    replies = iter([httpx.ConnectError("down"), 503, 429])

    def handler(request: httpx.Request) -> httpx.Response:
        reply = next(replies, None)
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
            return httpx.Response(reply, headers={"retry-after": "2"} if reply == 429 else {})
        return _ok(request)

    assert len((await _http(handler).analyze(_request())).suggestions) == 2
    assert no_sleep == [1.0, 2.0, 2.0]


async def test_http_client_error_is_final(no_sleep: list[float]) -> None:
    with pytest.raises(LLMInvalidResponseError) as error:
        await _http(lambda r: httpx.Response(422, text=SECRET_TEXT)).analyze(_request())
    assert error.value.status_code == 422 and SECRET_TEXT not in str(error.value)
    assert no_sleep == []


async def test_http_exhausted_retries_become_transient_error(no_sleep: list[float]) -> None:
    with pytest.raises(LLMTimeoutError, match="попыток: 2"):
        await _http(lambda r: httpx.Response(502), max_retries=1).analyze(_request())


async def test_http_async_mode_polls_until_completed(no_sleep: list[float]) -> None:
    polls = iter([httpx.Response(202), httpx.Response(200, json={"status": "running"})])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.raw_path.decode()}")
        if request.method == "POST":
            return httpx.Response(202, json={"request_id": "ml:42"})
        return next(polls, None) or httpx.Response(
            200, json=_response(request_id=_request().request_id)
        )

    result = await _http(handler, mode="async", poll_seconds=7).analyze(_request())
    assert len(result.suggestions) == 2
    assert seen == ["POST /v2/analyze"] + ["GET /v2/analyze/ml%3A42"] * 3
    assert no_sleep == [7, 7, 7]


async def test_http_deadline_covers_polling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={})
        return httpx.Response(200, json={"status": "queued"})

    with pytest.raises(LLMTimeoutError, match="0.2"):
        await _http(handler, mode="async", poll_seconds=0.01, wait_seconds=0.2).analyze(_request())


async def test_http_health_check() -> None:
    # Без ML_HEALTH_URL проверить нечем: «неизвестно», а не «здоров».
    assert await _http(_ok).health_check() is None
    assert (
        await _http(
            lambda r: httpx.Response(200), health_url="http://ml.test/health"
        ).health_check()
        is True
    )
    assert (
        await _http(
            lambda r: httpx.Response(503), health_url="http://ml.test/health"
        ).health_check()
        is False
    )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await _http(down, health_url="http://ml.test/health").health_check() is False


# --- заглушка ---------------------------------------------------------------------------


async def test_stub_suggestions_pass_verbatim_checks() -> None:
    request = _request()
    result = await StubAnalyzer().analyze(request)
    mapped = map_result(result, request.document.blocks)
    assert mapped.dropped == {}
    assert [(d.block_ref, d.change_type.value) for d in mapped.suggestions] == [
        ("p-1", "modify"),
        ("p-3", "add"),
    ]
    assert await StubAnalyzer().health_check()


# --- фабрика и настройки ----------------------------------------------------------------

_BASE_ENV = {
    "ENV": "local",
    "DATABASE_URL": "postgresql+asyncpg://app:db-strong-password-8391@db/app",
    "REDIS_URL": "redis://redis:6379/0",
    "S3_ENDPOINT": "seaweedfs:8333",
    "S3_ACCESS_KEY": "app-access",
    "S3_SECRET_KEY": "s3-strong-secret-8391",
    "S3_BUCKET": "bucket",
    "JWT_SECRET": "jwt-strong-secret-8391-jwt-strong-secret-8391",
    "CORS_ALLOWED_ORIGINS": "https://app.example.test",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **override: str) -> Settings:
    for key in list(_BASE_ENV) + [
        k for k in os.environ if k.startswith(("ML_", "ANALYSIS_", "LLM_"))
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_BASE_ENV, **override}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_defaults_keep_legacy_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    assert settings.analysis_provider == "legacy_llm"
    assert not uses_document_analyzer(settings)
    with pytest.raises(ValueError, match="legacy_llm"):
        get_document_analyzer(settings)
    limits = analysis_limits(settings)
    assert (limits.max_blocks, limits.max_block_chars, limits.max_sources) == (1000, 20000, 20)


def test_factory_builds_each_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(
        get_document_analyzer(_settings(monkeypatch, ANALYSIS_PROVIDER="stub")), StubAnalyzer
    )
    plugin = _settings(
        monkeypatch, ANALYSIS_PROVIDER="ml_plugin", ML_PLUGIN_ENTRYPOINT="json:loads"
    )
    assert isinstance(get_document_analyzer(plugin), PluginAnalyzer)
    http = _settings(
        monkeypatch, ANALYSIS_PROVIDER="ml_http", ML_ENDPOINT="http://ml:8080/v2/analyze"
    )
    assert isinstance(get_document_analyzer(http), HttpAnalyzer)
    assert uses_document_analyzer(http)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"ANALYSIS_PROVIDER": "ml_plugin"}, "ML_PLUGIN_ENTRYPOINT"),
        ({"ANALYSIS_PROVIDER": "ml_http"}, "ML_ENDPOINT"),
        ({"ML_WAIT_SECONDS": "1800"}, "ML_WAIT_SECONDS"),
        # Больше visibility timeout брокера задача и пауза перед повтором быть не могут.
        ({"ANALYSIS_TASK_TIME_LIMIT_SECONDS": "3600"}, "analysis_task_time_limit_seconds"),
        ({"ANALYSIS_RETRY_LIMIT": "7"}, "analysis_retry_limit"),
        (
            {
                "ENV": "production",
                "ANALYSIS_PROVIDER": "ml_http",
                "ML_ENDPOINT": "http://ml/v2/analyze",
                "ML_API_KEY": "change-me",
            },
            "ML_API_KEY",
        ),
    ],
)
def test_inconsistent_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(monkeypatch, **override)


def test_wait_must_leave_room_inside_task_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch, ML_WAIT_SECONDS="1700", ANALYSIS_TASK_TIME_LIMIT_SECONDS="3000"
    )
    assert settings.ml_wait_seconds == 1700
