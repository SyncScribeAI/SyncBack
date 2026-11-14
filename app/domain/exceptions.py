"""Доменные исключения SyncBack.

Каждое исключение соответствует одной бизнес-ситуации и конвертируется
в HTTP-ответ на уровне роутера или глобального exception handler.
"""


class SyncBackError(Exception):
    """Base class for expected failures handled by the API layer."""


# Алиас — всё доменное наследуется от DomainError
DomainError = SyncBackError


# ---------------------------------------------------------------------------
# Document / Project / Source
# ---------------------------------------------------------------------------


class DocumentNotFoundError(DomainError):
    """Документ не найден или не принадлежит проекту."""


class ProjectNotFoundError(DomainError):
    pass


class InvalidProjectColorError(DomainError):
    """Цвет карточки проекта не входит в палитру."""


class SourceNotFoundError(DomainError):
    pass


class SourceSnapshotNotFoundError(DomainError):
    pass


class InvalidSourceScopeError(DomainError):
    """Источник со scope=document нельзя создать без документа."""


class InvalidSourceUrlError(DomainError):
    """Ссылка на источник не проходит правила домена: схема, хост, приватный адрес."""


class SourceFetchError(DomainError):
    """Содержимое источника по ссылке получить не удалось: редиректы, размер, ответ сервера."""


class SourceRequiresAuthError(SourceFetchError):
    """Страница по ссылке открывается только после входа пользователя."""

    def __init__(
        self,
        message: str = "Страница требует входа. Сохраните её как файл и загрузите как источник",
    ) -> None:
        super().__init__(message)


class FileTooLargeError(DomainError):
    pass


class UnsupportedFormatError(DomainError):
    pass


UnsupportedFileFormatError = UnsupportedFormatError


class UnsupportedExportFormatError(DomainError):
    """Запрошенный формат экспорта невозможен для этого документа."""


# ---------------------------------------------------------------------------
# Document status transitions
# ---------------------------------------------------------------------------


class InvalidDocumentStatusError(DomainError):
    """Операция недопустима для текущего статуса документа.

    Сигнализирует роутеру вернуть HTTP 409 Conflict.
    """


class DocumentParseError(DomainError):
    pass


# ---------------------------------------------------------------------------
# Analysis jobs
# ---------------------------------------------------------------------------


class AnalysisJobNotFoundError(DomainError):
    """Задание анализа не найдено."""


class JobNotFoundError(AnalysisJobNotFoundError):
    """job_id передан в воркер или сервис, но запись в БД отсутствует.

    Отличие от AnalysisJobNotFoundError: используется в контекстах,
    где job обязан существовать (например, внутри Celery-задачи после
    dispatch). Позволяет отличить «не существует» от «уже завершён».
    """


class AnalysisAlreadyRunningError(DomainError):
    """Для документа уже выполняется анализ — нельзя запустить повторно.

    Сигнализирует роутеру вернуть HTTP 409 Conflict.
    """


class AnalysisJobConflictError(DomainError):
    """Вставка задачи нарушила уникальность в БД: параллельный запуск анализа.

    Бросается репозиторием; сервис превращает её в AnalysisAlreadyRunningError
    или AnalysisJobAlreadyExistsError. by_idempotency_key=True означает, что
    Idempotency-Key уже занят параллельным запросом, False — что у документа
    уже есть активная задача.
    """

    def __init__(self, message: str, *, by_idempotency_key: bool = False) -> None:
        super().__init__(message)
        self.by_idempotency_key = by_idempotency_key


class AnalysisJobAlreadyExistsError(DomainError):
    """Идемпотентный повтор: задачу с этим Idempotency-Key уже создал параллельный запрос.

    Сигнализирует роутеру вернуть HTTP 200 с уже существующей задачей.
    """

    def __init__(self, job: object) -> None:
        super().__init__("Задача анализа с этим Idempotency-Key уже создана")
        self.job = job


class AnalysisConfirmationRequiredError(DomainError):
    """Повторный анализ готового документа сбросит результаты ревью: нужен force."""


class AnalysisJobNotCancellableError(DomainError):
    """Задание анализа нельзя отменить (уже завершено или отменено).

    Сигнализирует роутеру вернуть HTTP 422 Unprocessable Entity.
    """


# ---------------------------------------------------------------------------
# Suggestions / Review
# ---------------------------------------------------------------------------


class SuggestionNotFoundError(DomainError):
    """Правка не найдена в БД вообще."""


class StaleSuggestionJobError(SuggestionNotFoundError):
    """Правка существует, но принадлежит устаревшему analysis job.

    Отличает «правки нет» от «правка из прежнего анализа»: клиент должен перезагрузить список правок — документ был
    переанализирован с тех пор, как клиент получил suggestion_id.
    Возвращает HTTP 404 (наследует SuggestionNotFoundError).
    """


class AnalysisFlagNotFoundError(DomainError):
    """Флага ручной проверки нет у этого документа."""


class StaleAnalysisFlagJobError(AnalysisFlagNotFoundError):
    """Флаг из прежнего анализа документа: клиенту нужно перечитать редактор.

    Наследует AnalysisFlagNotFoundError и отдаётся как 404 — так же, как правка
    прежнего анализа (StaleSuggestionJobError).
    """


class SuggestionAlreadyDecidedError(DomainError):
    """Правка уже была принята/отклонена другим запросом (race condition).

    Сигнализирует роутеру вернуть HTTP 409 Conflict.
    """


class SuggestionResetNotAllowedError(DomainError):
    """Нельзя сбросить решение по правке.

    Возникает когда правка уже в статусе PENDING (сбрасывать нечего)
    или когда документ не в статусе awaiting_approval.
    Сигнализирует роутеру вернуть HTTP 409 Conflict.
    """


class ReviewNotCompleteError(DomainError):
    """Review нельзя завершить: не все правки рассмотрены или экспорт не удался.

    Сигнализирует роутеру вернуть HTTP 422 Unprocessable Entity.
    """


class ReviewVersionConflictError(DomainError):
    """review_version в БД изменилась, пока клиент редактировал документ.

    Сигнализирует роутеру вернуть HTTP 409 Conflict.
    """


# Алиасы для обратной совместимости
StaleReviewVersionError = ReviewVersionConflictError
OptimisticLockError = ReviewVersionConflictError


# ---------------------------------------------------------------------------
# Auth / Credentials
# ---------------------------------------------------------------------------


class InvalidCredentialsError(DomainError):
    pass


class InvalidTokenError(DomainError):
    pass


class EmailAlreadyRegisteredError(DomainError):
    pass


class InvalidPasswordError(DomainError):
    """Пароль не удовлетворяет PasswordPolicy."""


class AccountTemporarilyLockedError(DomainError):
    """Слишком много неудачных попыток входа; retry_after — секунд до следующей попытки."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Слишком много неудачных попыток входа, повторите через {retry_after} с")
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Sources / Infrastructure
# ---------------------------------------------------------------------------


class SourceLockError(DomainError):
    """Изменение источников запрещено, пока документ анализируется (IN_PROGRESS)."""


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class LLMTimeoutError(DomainError):
    pass


class LLMInvalidResponseError(DomainError):
    """LLM отклонила запрос или вернула ответ, который нельзя разобрать.

    status_code — HTTP-код ответа, если ошибка пришла от сервера; None — ответ
    получен, но не прошёл разбор.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMInputTooLargeError(DomainError):
    """Документ и источник не помещаются в контекст модели."""
