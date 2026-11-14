"""Порты репозиториев.

Только чистые Python-типы и доменные value objects; ORM-модели упоминаются лишь
в аннотациях под TYPE_CHECKING. Реализации — app/infrastructure/db/repositories.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar

from app.domain.analysis_reuse import AnalysisStrategy, Baseline, RunInput
from app.domain.baseline_result import BaselineFlag, BaselineSuggestion
from app.domain.interfaces.entities import DocumentProtocol, SuggestionProtocol
from app.domain.interfaces.user_repository import IUserRepository  # noqa: F401
from app.domain.pipeline_run import PipelineRunSummary
from app.domain.source_snapshot import (
    JobSourceRecord,
    JobSourceSnapshot,
    SnapshotDraft,
    SourceSnapshot,
)
from app.domain.value_objects import (
    AnalysisJobStatusVO,
    DocumentFormatVO,
    DocumentStatusVO,
    FlagStatusVO,
    ProjectContentCounts,
    SourceScopeVO,
    SuggestionGroupSummary,
    SuggestionStatusVO,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.analysis_flag import AnalysisFlag
    from app.infrastructure.db.models.analysis_job import AnalysisJob
    from app.infrastructure.db.models.audit_log import AuditLog
    from app.infrastructure.db.models.document import Document
    from app.infrastructure.db.models.project import Project
    from app.infrastructure.db.models.source import Source
    from app.infrastructure.db.models.suggestion import Suggestion

# Сервисы домена держат документ и правку как протоколы, а вызывающие из инфраструктуры
# как ORM-объекты. Метод возвращает тот же объект, поэтому тип сохраняется у обоих.
DocumentT = TypeVar("DocumentT", bound=DocumentProtocol)
SuggestionT = TypeVar("SuggestionT", bound=SuggestionProtocol)


class DocumentRow(TypedDict):
    """Контракт строки результата list_all_for_user.

    Каждый dict в списке содержит объект Document + агрегаты
    из SQL-запроса (LEFT JOIN на projects и COUNT правок).
    Поле sources НЕ включено сюда намеренно — оно инъектируется отдельным
    батч-запросом в роутере через SourceService.list_sources_for_documents.
    """

    document: Document
    project_name: str
    suggestions_total: int
    suggestions_pending: int
    suggestions_accepted: int
    suggestions_rejected: int


class IDocumentRepository(ABC):
    @abstractmethod
    async def get_by_id(self, document_id: uuid.UUID) -> Document | None: ...

    @abstractmethod
    async def list_all_for_user(
        self,
        user_id: uuid.UUID,
        limit: int,
        offset: int,
        status: DocumentStatusVO | None = None,
        outdated: bool = False,
        search: str | None = None,
        sort_by: str = "updated_at",
        sort_dir: str = "desc",
    ) -> tuple[list[DocumentRow], int]:
        """Вернуть (список документо-строк, total_count) за один round-trip.

        Параметры фильтрации/сортировки приведены к реализации DocumentRepository.
        total_count включён в кортеж — отдельный count_for_user не нужен.
        """
        ...

    # Обобщённого update нет: все мутации идут через специализированные методы ниже.

    @abstractmethod
    async def create(
        self,
        *,
        id: uuid.UUID,
        project_id: uuid.UUID,
        name: str,
        format: DocumentFormatVO,
        storage_key: str,
        size_bytes: int,
    ) -> Document: ...

    @abstractmethod
    async def get_many_by_ids(self, document_ids: list[uuid.UUID]) -> list[Document]: ...

    @abstractmethod
    async def update_status(self, document: DocumentT, status: DocumentStatusVO) -> DocumentT: ...

    @abstractmethod
    async def compare_and_set_status(
        self,
        document_id: uuid.UUID,
        analysis_job_id: uuid.UUID | None,
        expected: DocumentStatusVO,
        target: DocumentStatusVO,
    ) -> bool:
        """Сменить статус, только если в БД всё ещё expected и тот же текущий анализ.

        False — документ успел изменить параллельный запрос (например, повторный анализ).
        Загруженный в сессию объект документа получает новый статус.
        """
        ...

    @abstractmethod
    async def set_current_job(self, document: Document, job_id: uuid.UUID | None) -> Document:
        """Назначить документу текущий (последний запущенный) анализ."""
        ...

    @abstractmethod
    async def update_exported_key(self, document: DocumentProtocol, export_key: str) -> None: ...

    @abstractmethod
    async def compare_and_increment_review_version(
        self,
        document_id: uuid.UUID,
        expected_version: int,
    ) -> Document | None:
        """Инкрементировать review_version, если она равна expected_version.

        Возвращает обновлённый документ или None при расхождении версий.
        """
        ...

    @abstractmethod
    async def delete(self, document: Document) -> None:
        """Удалить уже загруженный документ."""
        ...

    @abstractmethod
    async def delete_by_id(
        self, document_id: uuid.UUID, project_id: uuid.UUID
    ) -> dict[str, str | None] | None:
        """Удалить документ проекта одним запросом без предварительной загрузки.

        Возвращает ключи файлов в хранилище
        ({"storage_key", "original_storage_key", "exported_storage_key"})
        для последующей очистки или None, если документа в проекте нет.
        Значение каждого ключа может быть None.
        """
        ...

    @abstractmethod
    async def delete_document_scoped_sources(self, document_id: uuid.UUID) -> list[str]:
        """Удалить источники со scope=document, прикреплённые к документу.

        Возвращает ключи файлов удалённых источников для очистки хранилища после commit.
        """
        ...

    # --- Методы запросов, не связанные с мутациями ---

    @abstractmethod
    async def list_for_project(
        self,
        project_id: uuid.UUID,
        pagination: Any,
        *,
        status: DocumentStatusVO | None = None,
    ) -> list[Document]: ...

    @abstractmethod
    async def count_for_project(
        self,
        project_id: uuid.UUID,
        *,
        status: DocumentStatusVO | None = None,
    ) -> int: ...

    @abstractmethod
    async def list_ids_by_statuses(
        self, project_id: uuid.UUID, statuses: frozenset[DocumentStatusVO]
    ) -> list[uuid.UUID]: ...

    @abstractmethod
    async def get_stats_for_project(
        self,
        project_id: uuid.UUID,
    ) -> dict[str, int]: ...


class ISourceRepository(ABC):
    @abstractmethod
    async def get_by_id(self, source_id: uuid.UUID) -> Source | None: ...

    @abstractmethod
    async def get_many_by_ids(self, source_ids: list[uuid.UUID]) -> list[Source]: ...

    @abstractmethod
    async def list_for_analysis(
        self, project_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[Source]:
        """Источники, участвующие в анализе документа: базовые источники проекта
        плюс прикреплённые к документу. Порядок стабильный (created_at, id).
        """
        ...

    @abstractmethod
    async def list_by_project(
        self,
        project_id: uuid.UUID,
        limit: int,
        offset: int,
        scope: SourceScopeVO | None = None,
    ) -> list[Source]: ...

    @abstractmethod
    async def count_by_project(
        self, project_id: uuid.UUID, scope: SourceScopeVO | None = None
    ) -> int: ...

    @abstractmethod
    async def list_by_document_ids(
        self,
        project_id: uuid.UUID,
        document_ids: list[uuid.UUID],
    ) -> list[tuple[Source, uuid.UUID]]:
        """Документные источники для набора документов в виде пар (source, document_id)."""
        ...

    @abstractmethod
    async def list_attached_document_ids(self, source_id: uuid.UUID) -> list[uuid.UUID]:
        """Документы, к которым прикреплён источник."""
        ...

    @abstractmethod
    async def attach_to_document(self, source_id: uuid.UUID, document_id: uuid.UUID) -> None:
        """Прикрепить источник к документу; повторный вызов безопасен."""
        ...

    @abstractmethod
    async def create_with_id(
        self,
        source_id: uuid.UUID,
        project_id: uuid.UUID,
        name: str,
        source_type: Any,
        storage_key: str,
        scope: Any,
    ) -> Source: ...

    @abstractmethod
    async def create_url(
        self,
        project_id: uuid.UUID,
        name: str,
        url: str,
        scope: Any,
    ) -> Source: ...

    @abstractmethod
    async def delete(self, source: Source) -> None: ...


class IAnalysisFlagRepository(ABC):
    """Флаги «проверьте вручную» одного анализа."""

    @abstractmethod
    async def bulk_create(self, flags: Sequence[AnalysisFlag]) -> None: ...

    @abstractmethod
    async def carry_over(
        self,
        from_job_id: uuid.UUID,
        to_job_id: uuid.UUID,
        only: Sequence[uuid.UUID] | None = None,
    ) -> int:
        """Скопировать результат прошлой задачи в новую вместе с решениями; число строк.

        only ограничивает копию выбранными флагами.
        """
        ...

    @abstractmethod
    async def baseline_rows(self, job_id: uuid.UUID) -> list[BaselineFlag]:
        """Сводка флагов задачи для правил инкрементального переноса."""
        ...

    @abstractmethod
    async def get_by_id(self, flag_id: uuid.UUID) -> AnalysisFlag | None: ...

    @abstractmethod
    async def list_by_analysis_job(self, analysis_job_id: uuid.UUID) -> list[AnalysisFlag]:
        """Все флаги анализа в порядке ответа конвейера (position)."""
        ...

    @abstractmethod
    async def set_status(
        self, flag: AnalysisFlag, status: FlagStatusVO, user_id: uuid.UUID
    ) -> bool:
        """Сменить статус одним UPDATE ... WHERE status <> :status.

        False — флаг уже в этом статусе; повторное нажатие не пишет второе событие аудита.
        """
        ...

    @abstractmethod
    async def reset_to_pending(self, analysis_job_id: uuid.UUID) -> int:
        """Вернуть просмотренные флаги анализа в pending; число затронутых строк."""
        ...


class ISuggestionRepository(ABC):
    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_by_id(self, suggestion_id: uuid.UUID) -> Suggestion | None: ...

    @abstractmethod
    async def list_by_analysis_job(
        self,
        analysis_job_id: uuid.UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
        status: SuggestionStatusVO | None = None,
    ) -> list[Suggestion]: ...

    @abstractmethod
    async def list_with_total(
        self,
        analysis_job_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: SuggestionStatusVO | None = None,
        group_key: str | None = None,
    ) -> tuple[list[Suggestion], int]:
        """Страница и total одним SELECT с оконной функцией.

        func.count().over() вычисляется ДО применения LIMIT/OFFSET в PostgreSQL,
        поэтому возвращает полный COUNT фильтрованной выборки.
        """
        ...

    @abstractmethod
    async def list_groups(self, analysis_job_id: uuid.UUID) -> list[SuggestionGroupSummary]:
        """Группы правок задачи с размером и числом нерешённых, одним GROUP BY."""
        ...

    @abstractmethod
    async def count_by_analysis_job(self, analysis_job_id: uuid.UUID) -> int: ...

    @abstractmethod
    async def count_by_analysis_job_and_status(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
    ) -> int: ...

    @abstractmethod
    async def list_by_analysis_job_and_status(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
    ) -> list[Suggestion]: ...

    @abstractmethod
    async def list_by_analysis_job_and_status_page(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
        limit: int,
        offset: int = 0,
    ) -> list[Suggestion]: ...

    @abstractmethod
    async def list_ids_by_analysis_job_and_status(
        self,
        analysis_job_id: uuid.UUID,
        status: SuggestionStatusVO,
    ) -> list[uuid.UUID]: ...

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    @abstractmethod
    async def bulk_create(self, suggestions: list[Suggestion]) -> list[Suggestion]: ...

    @abstractmethod
    async def carry_over(
        self,
        from_job_id: uuid.UUID,
        to_job_id: uuid.UUID,
        only: Sequence[uuid.UUID] | None = None,
        stale: Sequence[uuid.UUID] = (),
    ) -> int:
        """Скопировать результат прошлой задачи в новую вместе с решениями и связями с источниками; число строк.

        only ограничивает копию выбранными правками; копии правок из stale помечаются
        устаревшими.
        """
        ...

    @abstractmethod
    async def baseline_rows(self, job_id: uuid.UUID) -> list[BaselineSuggestion]:
        """Сводка правок задачи для правил инкрементального переноса."""
        ...

    @abstractmethod
    async def update_status(
        self,
        suggestion: Suggestion,
        decision: Any,
    ) -> None:
        """Атомарный UPDATE ... WHERE status = 'pending'.

        Бросает SuggestionAlreadyDecidedError, если строка не затронута.
        """
        ...

    @abstractmethod
    async def bulk_update_status(
        self,
        decisions: Any,
    ) -> int:
        """Один UPDATE ... WHERE id IN (...) AND status = 'pending'.

        Область ограничена документом: decisions.document_id сравнивается
        с suggestions.document_id.
        """
        ...

    @abstractmethod
    async def bulk_accept_all(
        self,
        analysis_job_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[Suggestion]:
        """Принять все PENDING-правки одним UPDATE, вернуть обновлённые объекты."""
        ...

    @abstractmethod
    async def bulk_reject_all(
        self,
        analysis_job_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[Suggestion]:
        """Отклонить все PENDING-правки одним UPDATE.

        Зеркало bulk_accept_all — один UPDATE WHERE status=PENDING,
        затем SELECT обновлённых объектов.
        """
        ...

    @abstractmethod
    async def reset_status(
        self,
        suggestion: SuggestionT,
    ) -> SuggestionT | None:
        """Сбросить решение правки обратно в PENDING.

        Атомарный UPDATE WHERE status != PENDING AND id = ?.
        Возвращает обновлённый объект или None если правка уже PENDING
        (сбрасывать нечего — идемпотентно со стороны репозитория).
        """
        ...

    @abstractmethod
    async def reset_to_pending(
        self,
        analysis_job_id: uuid.UUID,
        ids: Sequence[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        """Вернуть в PENDING принятые/отклонённые правки job (все или только ids).

        Возвращает id фактически сброшенных правок.
        """
        ...


class IAnalysisJobRepository(ABC):
    @abstractmethod
    async def get_by_id(self, job_id: uuid.UUID) -> AnalysisJob | None: ...

    @abstractmethod
    async def list_by_ids(self, job_ids: Sequence[uuid.UUID]) -> list[AnalysisJob]:
        """Задачи по списку id одним запросом, в произвольном порядке."""
        ...

    @abstractmethod
    async def get_by_idempotency_key(
        self, document_id: uuid.UUID, idempotency_key: str
    ) -> AnalysisJob | None: ...

    @abstractmethod
    async def get_active_by_document_id(self, document_id: uuid.UUID) -> AnalysisJob | None: ...

    @abstractmethod
    async def list_by_document(
        self, document_id: uuid.UUID, pagination: Any
    ) -> list[AnalysisJob]: ...

    @abstractmethod
    async def create_for_document(
        self,
        document: Document,
        *,
        job_id: uuid.UUID | None = None,
        status: AnalysisJobStatusVO = AnalysisJobStatusVO.PENDING,
        idempotency_key: str | None = None,
        strategy: AnalysisStrategy = AnalysisStrategy.AUTO,
    ) -> AnalysisJob: ...

    @abstractmethod
    async def set_celery_task_id(self, job: AnalysisJob, task_id: str) -> AnalysisJob: ...

    @abstractmethod
    async def mark_processing_if_active(self, job_id: uuid.UUID) -> bool:
        """Атомарно перевести незавершённую задачу в PROCESSING; False, если она уже завершена."""
        ...

    @abstractmethod
    async def lock_if_active(self, job_id: uuid.UUID) -> AnalysisJob | None:
        """Незавершённая задача под блокировкой строки до конца транзакции; иначе None.

        Запись результата и смена статуса под этой блокировкой не дают двум доставкам
        одной задачи сохранить правки дважды.
        """
        ...

    @abstractmethod
    async def list_unqueued_before(self, cutoff: datetime, limit: int) -> list[uuid.UUID]:
        """Незапущенные задачи без идентификатора в очереди, созданные раньше cutoff.

        Такая задача осталась от процесса, упавшего между записью статуса и отправкой в
        брокер. Старые идут первыми: они дольше всех блокируют повторный запуск.
        """
        ...

    @abstractmethod
    async def lock_unqueued(self, job_id: uuid.UUID, cutoff: datetime) -> AnalysisJob | None:
        """Задача из list_unqueued_before под блокировкой строки; None, если её уже взяли.

        Занятая строка пропускается без ожидания: её отправляет другая сверка или
        отменяет пользователь.
        """
        ...

    @abstractmethod
    async def record_pipeline_run(self, job: AnalysisJob, run: PipelineRunSummary) -> None:
        """Сохранить сводку вызова ML вместе с задачей; фиксация — на вызывающем."""
        ...

    @abstractmethod
    async def find_reuse_baseline(
        self, document_id: uuid.UUID, exclude_job_id: uuid.UUID
    ) -> Baseline | None:
        """Последняя полная задача документа с хэшем входа: success без упавших блоков."""
        ...

    @abstractmethod
    async def record_input(self, job: AnalysisJob, run_input: RunInput) -> None:
        """Хэш входа, решение о переносе и объём отправленного в ML вместе с задачей."""
        ...

    @abstractmethod
    async def record_reuse(self, job: AnalysisJob, baseline_job_id: uuid.UUID) -> None:
        """Сводка перенесённого результата: счётчики блоков и оснований прошлой задачи."""
        ...

    @abstractmethod
    async def record_audit(self, job_id: uuid.UUID, audit: dict[str, Any]) -> None:
        """Итог контрольного полного анализа в meta уже завершённой задачи."""
        ...

    @abstractmethod
    async def update_status(
        self,
        job: AnalysisJob,
        status: AnalysisJobStatusVO,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AnalysisJob:
        """Записать статус (переход уже проверен вызывающим) и отметки времени."""
        ...


class IAuditLogRepository(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID | None,
        action: str,
        suggestion_id: uuid.UUID | None = None,
        details: Any | None = None,
    ) -> AuditLog: ...

    @abstractmethod
    async def create_many(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID | None,
        action: str,
        suggestion_ids: Sequence[uuid.UUID],
    ) -> int:
        """Записать одно и то же действие для набора правок одним flush."""
        ...

    @abstractmethod
    async def list_for_document(
        self, document_id: uuid.UUID, limit: int, offset: int
    ) -> list[AuditLog]: ...


class IProjectRepository(ABC):
    @abstractmethod
    async def get_by_id(self, project_id: uuid.UUID) -> Project | None: ...

    @abstractmethod
    async def get_for_user(self, project_id: uuid.UUID, owner_id: uuid.UUID) -> Project:
        """Проект владельца; если не найден или чужой — ProjectNotFoundError."""

    @abstractmethod
    async def create(
        self,
        owner_id: uuid.UUID,
        name: str,
        description: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> Project:
        """Создать проект; сущность собирает инфраструктура."""

    @abstractmethod
    async def list_all(self, limit: int, offset: int) -> list[Project]: ...

    @abstractmethod
    async def count_all(self) -> int: ...

    @abstractmethod
    async def list_by_owner(
        self, owner_id: uuid.UUID, limit: int, offset: int
    ) -> list[Project]: ...

    @abstractmethod
    async def count_by_owner(self, owner_id: uuid.UUID) -> int: ...

    @abstractmethod
    async def update(
        self,
        project: Project,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> Project: ...

    @abstractmethod
    async def count_contents(
        self, project_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ProjectContentCounts]:
        """Счётчики документов и project-scope источников; проекты без них в словарь не попадают."""

    @abstractmethod
    async def collect_storage_keys(self, project_id: uuid.UUID) -> list[str]: ...

    @abstractmethod
    async def delete(self, project: Project) -> None: ...


class ISourceSnapshotRepository(ABC):
    """Снимки источников и строки задач анализа; оперирует доменными типами без ORM."""

    @abstractmethod
    async def ensure_snapshots(self, drafts: Sequence[SnapshotDraft]) -> dict[str, uuid.UUID]:
        """Создаёт недостающие снимки и возвращает id по content_hash.

        Повторная запись того же текста ничего не меняет: снимок адресуется содержимым.
        """

    @abstractmethod
    async def add_job_sources(
        self, job_id: uuid.UUID, records: Sequence[JobSourceRecord], ids: dict[str, uuid.UUID]
    ) -> None: ...

    @abstractmethod
    async def list_job_sources(self, job_id: uuid.UUID) -> list[JobSourceSnapshot]: ...

    @abstractmethod
    async def get_in_project(
        self, project_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> SourceSnapshot | None: ...

    @abstractmethod
    async def delete_unreferenced(self, project_id: uuid.UUID) -> list[str]:
        """Удаляет снимки проекта без ссылок из задач и возвращает их ключи хранилища."""

    @abstractmethod
    async def list_storage_keys(self, project_id: uuid.UUID) -> list[str]: ...
