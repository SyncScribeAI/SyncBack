"""Единая точка чтения конфигурации приложения."""

import codecs
import ipaddress
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.source_url import SourceUrlPolicy

_WEAK_SECRET_VALUES = {
    "admin",
    "change-me",
    "changeme",
    "default",
    "minioadmin",
    "password",
    "postgres",
    "root",
    "secret",
    "syncscribe",
    "test",
}
_WEAK_SECRET_PREFIXES = (
    "change-me",
    "changeme",
    "replace-me",
    "replace_with",
    "your-",
    "your_",
    "<",
)


def _is_placeholder_secret(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    return (
        normalized in _WEAK_SECRET_VALUES
        or normalized.startswith(_WEAK_SECRET_PREFIXES)
        or normalized.endswith("...")
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["local", "staging", "production"] = "local"
    debug: bool = False

    database_url: str
    redis_url: str
    redis_sse_channel: str = "syncscribe:sse"

    s3_endpoint: str = Field(validation_alias=AliasChoices("S3_ENDPOINT", "MINIO_ENDPOINT"))
    s3_access_key: str = Field(
        validation_alias=AliasChoices("S3_ACCESS_KEY", "MINIO_ACCESS_KEY", "MINIO_ROOT_USER")
    )
    s3_secret_key: str = Field(
        validation_alias=AliasChoices("S3_SECRET_KEY", "MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD")
    )
    s3_bucket: str = Field(validation_alias=AliasChoices("S3_BUCKET", "MINIO_BUCKET"))
    s3_secure: bool = Field(
        default=False, validation_alias=AliasChoices("S3_SECURE", "MINIO_SECURE")
    )
    s3_presigned_url_expire_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "S3_PRESIGNED_URL_EXPIRE_SECONDS", "MINIO_PRESIGNED_URL_EXPIRE_SECONDS"
        ),
    )

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30

    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    rate_limit_storage_uri: str = "memory://"
    max_upload_size_mb: int = 50

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    llm_provider: Literal["stub", "openai_compatible", "remote_http", "onprem"] = "stub"
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_auth: Literal["none", "bearer", "oauth_gigachat"] = "bearer"
    llm_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    llm_oauth_scope: str = "GIGACHAT_API_PERS"
    llm_ca_bundle: str = ""
    llm_verify_ssl: bool = True
    llm_temperature: float = 0.1
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 3
    llm_max_input_chars: int = 200_000

    @model_validator(mode="after")
    def _validate_llm(self) -> "Settings":
        if self.llm_provider == "stub":
            return self
        if not self.llm_endpoint:
            raise ValueError("LLM_ENDPOINT обязателен для LLM_PROVIDER != stub")
        if not self.llm_model:
            raise ValueError(
                "LLM_MODEL обязателен: у OpenAI-совместимых API нет модели по умолчанию"
            )
        if self.llm_auth != "none" and not self.llm_api_key:
            raise ValueError(f"LLM_API_KEY обязателен для LLM_AUTH={self.llm_auth}")
        return self

    # Анализ по контракту ML 2.0. legacy_llm — прежний конвейер «документ × источник» через
    # LLMClient; остальные провайдеры отправляют документ целиком в DocumentAnalyzer.
    analysis_provider: Literal["legacy_llm", "ml_plugin", "ml_http", "stub"] = "legacy_llm"
    ml_plugin_entrypoint: str = ""
    ml_endpoint: str = ""
    ml_health_url: str = ""
    ml_api_key: str = ""
    ml_http_mode: Literal["sync", "async"] = "sync"
    ml_poll_seconds: float = Field(default=5.0, gt=0)
    ml_wait_seconds: int = Field(default=1500, gt=0)
    ml_max_retries: int = Field(default=3, ge=0)
    # Верхние границы держат задачу и паузу перед повтором (30·2^n с) меньше visibility
    # timeout Redis-брокера (3600 с по умолчанию): иначе при task_acks_late брокер отдал бы
    # ту же задачу второму воркеру, и конвейер был бы вызван дважды.
    analysis_task_time_limit_seconds: int = Field(default=1800, gt=0, le=3000)
    analysis_retry_limit: int = Field(default=3, ge=0, le=6)
    analysis_max_blocks: int = Field(default=1000, gt=0)
    analysis_max_block_chars: int = Field(default=20_000, gt=0)
    analysis_max_source_chars: int = Field(default=200_000, gt=0)
    analysis_max_sources: int = Field(default=20, gt=0)
    analysis_source_fetch_concurrency: int = Field(default=4, gt=0)
    analysis_store_payloads: bool = False
    # Флаги «проверьте вручную» целиком уходят в агрегат редактора; лишние отбрасываются
    # с предупреждением в логе, чтобы ответ GET /editor оставался ограниченным.
    analysis_max_flags: int = Field(default=500, gt=0, le=5000)
    # Доля слов цитаты, которая должна найтись в сегменте источника, чтобы сегмент считался
    # её основанием. [гипотеза] 0.6 уточняется по первым реальным прогонам.
    analysis_evidence_min_containment: float = Field(default=0.6, gt=0, le=1)
    # Повторный анализ документа. off всегда вызывает ML полным запросом; reuse переносит
    # результат прошлой полной задачи, если вход совпал; delta вдобавок при изменённых
    # источниках проверяет заново только блоки, которых касаются изменения.
    analysis_incremental_mode: Literal["off", "reuse", "delta"] = "delta"
    # Пределы выборочной проверки; за ними задача идёт полным анализом. [гипотеза] Значения
    # уточняются по набору пар «база и изменение» на реальной модели.
    analysis_delta_max_changed_share: float = Field(default=0.3, ge=0, le=1)
    analysis_delta_max_blocks_share: float = Field(default=0.5, ge=0, le=1)
    # Сколько выборочных задач подряд допускается после полного анализа.
    analysis_delta_max_chain: int = Field(default=5, ge=0, le=100)
    # Отбор блоков по изменённому сегменту: лучшие N по перекрытию слов и все с
    # нормированной оценкой не ниже порога; редкое слово встречается не больше чем в
    # rare_blocks блоках. [гипотеза]
    analysis_delta_blocks_per_segment: int = Field(default=8, ge=1, le=200)
    analysis_delta_score_threshold: float = Field(default=0.3, gt=0, le=1)
    analysis_delta_rare_blocks: int = Field(default=2, ge=0, le=50)
    # Длина основы слова при сравнении; заменяет морфологию без словаря. [гипотеза]
    analysis_delta_stem_chars: int = Field(default=6, ge=3, le=32)
    # Соседи изменённого сегмента с каждой стороны и опорные неизменённые сегменты на блок.
    analysis_delta_context_segments: int = Field(default=2, ge=0, le=20)
    analysis_delta_support_segments: int = Field(default=3, ge=0, le=50)
    # Доля выборочных задач, после которых тот же вход проверяется полным анализом, а
    # расхождение пишется в meta задачи. 0 отключает аудит: он удваивает вызовы модели.
    analysis_delta_audit_rate: float = Field(default=0.0, ge=0, le=1)
    # Больше страниц PDF не разбирается: разбор держит общий замок PDFium, и один огромный
    # файл задержал бы все остальные.
    pdf_max_pages: int = Field(default=500, gt=0)
    # Столько файлов процесс разбирает одновременно. Остальные запросы ждут свободный поток,
    # а не занимают CPU и память все сразу; PDF всё равно идут по одному из-за замка PDFium.
    parse_pool_workers: int = Field(default=2, ge=1, le=32)
    # Автор аннотаций экспорта PDF (/T): просмотрщик показывает его в заметке правки.
    pdf_annotation_author: str = Field(default="SyncScribe", min_length=1, max_length=100)
    # Очередь Celery задачи анализа v2: её слушает отдельный воркер с числом процессов, равным
    # числу одновременных прогонов, которое выдерживает ML-конвейер. «celery» вернёт задачу в
    # общую очередь — для разработки с одним воркером.
    analysis_v2_queue: str = Field(default="analysis_v2", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    suggestion_low_confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    # Догоняющие правки для точных повторов фразы. Поиск — O(G·T) по тексту документа,
    # поэтому число групп и правок на группу ограничено сверху.
    suggestion_propagate_exact: bool = False
    suggestion_propagate_max_groups: int = Field(default=50, gt=0, le=500)
    suggestion_propagate_max_per_group: int = Field(default=50, gt=0, le=500)

    # Источники по ссылке. По умолчанию пускаются только публичные адреса. Внутренний
    # портал доступен, когда его имя есть в SOURCE_URL_ALLOWED_HOSTS (точное имя или
    # суффикс с ведущей точкой) и адрес входит в SOURCE_URL_ALLOWED_NETWORKS. Подсеть
    # Docker в список сетей не вносится: иначе ссылки дотянутся до сервисов compose.
    source_url_allowed_hosts: Annotated[list[str], NoDecode] = []
    source_url_allowed_networks: Annotated[list[str], NoDecode] = []
    source_url_allow_public: bool = True
    source_url_ca_bundle: str = ""
    source_url_auth_headers_file: str = ""
    source_url_user_agent: str = Field(
        default="SyncScribeBot/1.0 (+https://syncscribe.app/bot)", min_length=1, max_length=200
    )
    source_url_robots: Literal["off", "log", "enforce"] = "log"
    source_url_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    source_url_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    source_url_read_timeout_seconds: float = Field(default=20.0, gt=0)

    # Выделение основного содержимого страниц. Селектор корня задаётся по хосту JSON-объектом
    # {"wiki.corp": "#main-content"}; ключ с ведущей точкой подходит для поддоменов. Порог
    # доли текста и минимум символов являются гипотезами и проверяются на порталах владельца.
    source_html_content_selectors: dict[str, str] = {}
    source_html_min_kept_share: float = Field(default=0.1, ge=0, le=1)
    source_html_min_chars: int = Field(default=200, ge=0)
    source_html_fallback_encoding: str = "cp1251"

    @field_validator("source_html_fallback_encoding")
    @classmethod
    def _validate_fallback_encoding(cls, v: str) -> str:
        try:
            codecs.lookup(v)
        except LookupError as exc:
            raise ValueError(f"SOURCE_HTML_FALLBACK_ENCODING: неизвестная кодировка {v}") from exc
        return v

    @field_validator("source_html_content_selectors")
    @classmethod
    def _normalize_selector_hosts(cls, v: dict[str, str]) -> dict[str, str]:
        return {host.strip().rstrip(".").lower(): selector for host, selector in v.items()}

    @field_validator("source_url_allowed_hosts", "source_url_allowed_networks", mode="before")
    @classmethod
    def _parse_comma_list(cls, v: object) -> list[str]:
        if isinstance(v, str):
            v = [item.strip() for item in v.split(",") if item.strip()]
        if not isinstance(v, list):
            raise ValueError("ожидается список через запятую")
        return v

    @field_validator("source_url_allowed_networks")
    @classmethod
    def _validate_networks(cls, v: list[str]) -> list[str]:
        for network in v:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"SOURCE_URL_ALLOWED_NETWORKS: некорректная сеть {network}"
                ) from exc
        return v

    @property
    def source_url_policy(self) -> SourceUrlPolicy:
        return SourceUrlPolicy.from_lists(
            self.source_url_allowed_hosts,
            self.source_url_allowed_networks,
            allow_public=self.source_url_allow_public,
        )

    @model_validator(mode="after")
    def _validate_analysis(self) -> "Settings":
        if self.analysis_provider == "ml_plugin" and ":" not in self.ml_plugin_entrypoint:
            raise ValueError(
                "ML_PLUGIN_ENTRYPOINT обязателен для ANALYSIS_PROVIDER=ml_plugin "
                "в виде package.module:function"
            )
        if self.analysis_provider == "ml_http" and not self.ml_endpoint:
            raise ValueError("ML_ENDPOINT обязателен для ANALYSIS_PROVIDER=ml_http")
        # Дедлайн ожидания ML меньше лимита задачи: иначе Celery прервёт задачу раньше,
        # чем адаптер вернёт понятную ошибку, и запас уйдёт на загрузку источников и запись.
        if self.ml_wait_seconds >= self.analysis_task_time_limit_seconds:
            raise ValueError("ML_WAIT_SECONDS должен быть меньше ANALYSIS_TASK_TIME_LIMIT_SECONDS")
        return self

    @model_validator(mode="after")
    def _forbid_deployment_placeholders(self) -> "Settings":
        """Внешние окружения не должны запускаться с примерами из .env.example."""
        if self.env == "local":
            return self

        unsafe_fields: list[str] = []
        try:
            database_password = unquote(urlsplit(self.database_url).password or "")
        except ValueError:
            database_password = ""
        if _is_placeholder_secret(database_password):
            unsafe_fields.append("DATABASE_URL password")
        if _is_placeholder_secret(self.jwt_secret):
            unsafe_fields.append("JWT_SECRET")
        if _is_placeholder_secret(self.s3_secret_key):
            unsafe_fields.append("S3_SECRET_KEY")
        if (
            self.llm_provider != "stub"
            and self.llm_auth != "none"
            and _is_placeholder_secret(self.llm_api_key)
        ):
            unsafe_fields.append("LLM_API_KEY")
        if self.analysis_provider == "ml_http" and _is_placeholder_secret(self.ml_api_key):
            unsafe_fields.append("ML_API_KEY")

        if unsafe_fields:
            names = ", ".join(unsafe_fields)
            raise ValueError(f"ENV={self.env}: замените placeholder/стандартные значения: {names}")
        return self

    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    cors_allowed_origins: Annotated[list[str], NoDecode]

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> list[str]:
        if isinstance(v, str):
            v = [origin.strip() for origin in v.split(",") if origin.strip()]
        if not isinstance(v, list):
            raise ValueError("cors_allowed_origins должен быть списком origin")
        return v

    @field_validator("cors_allowed_origins")
    @classmethod
    def _forbid_wildcard_origin(cls, v: list[str]) -> list[str]:
        if "*" in v:
            raise ValueError(
                "cors_allowed_origins не может содержать «*»: укажите явные origin фронтенда"
            )
        if not v:
            raise ValueError("cors_allowed_origins не может быть пустым")
        return v

    trusted_proxy_hosts: Annotated[list[str], NoDecode] = []

    @field_validator("trusted_proxy_hosts", mode="before")
    @classmethod
    def _parse_trusted_proxies(cls, v: object) -> list[str]:
        if isinstance(v, str):
            v = [host.strip() for host in v.split(",") if host.strip()]
        if not isinstance(v, list):
            raise ValueError("trusted_proxy_hosts должен быть списком адресов")
        if "*" in v:
            raise ValueError(
                "trusted_proxy_hosts не может содержать «*»: тогда любой клиент "
                "подменит свой адрес заголовком X-Forwarded-For"
            )
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "jwt_secret должен содержать минимум 32 символа. "
                "Сгенерируйте его командой: openssl rand -hex 32"
            )
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
