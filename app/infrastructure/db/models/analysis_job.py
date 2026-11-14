"""
Путь в репозитории: app/infrastructure/db/models/analysis_job.py

idempotency_key уникален в пределах документа (uq_analysis_jobs_doc_idem_key):
все запросы идут через get_by_idempotency_key(document_id, key), поэтому отдельный
индекс по ключу не нужен.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.models.enums import AnalysisJobStatus

if TYPE_CHECKING:
    from app.infrastructure.db.models.document import Document
    from app.infrastructure.db.models.suggestion import Suggestion


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        # Один ключ идемпотентности может относиться только к одному документу.
        # NULL-значения не нарушают уникальность в PostgreSQL.
        UniqueConstraint("document_id", "idempotency_key", name="uq_analysis_jobs_doc_idem_key"),
        # У документа не может быть двух незавершённых задач одновременно.
        Index(
            "uq_analysis_jobs_one_active_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'dispatched', 'processing')"),
            sqlite_where=text("status IN ('pending', 'dispatched', 'processing')"),
        ),
        # Последняя задача документа: WHERE document_id = ? ORDER BY created_at DESC.
        Index("ix_analysis_jobs_doc_latest", "document_id", "created_at"),
        CheckConstraint("strategy IN ('auto', 'full')", name="ck_analysis_jobs_strategy"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[AnalysisJobStatus] = mapped_column(
        sa.Enum(
            AnalysisJobStatus,
            name="analysis_job_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AnalysisJobStatus.PENDING,
        server_default=AnalysisJobStatus.PENDING.value,
    )
    # Ключ идемпотентности из заголовка Idempotency-Key.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    partial_success: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default="false"
    )
    # Сводка вызова ML 2.0; у задач legacy_llm поля остаются NULL.
    # request_id — для поиска запроса в логах harness.
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # {"contract_version", "provider", "meta"}; meta сверх лимита не сохраняется.
    pipeline_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # sha256 входа анализа (app.domain.services.analysis_reuse.analysis_input_hash);
    # NULL у задач legacy_llm и у задач, не дошедших до загрузки источников.
    input_hash: Mapped[str | None] = mapped_column(sa.CHAR(64), nullable=True)
    # auto разрешает перенос результата прошлой задачи, full всегда вызывает ML.
    strategy: Mapped[str] = mapped_column(
        String(8), nullable=False, default="auto", server_default="auto"
    )
    checked_blocks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_blocks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def incremental_view(self) -> dict[str, Any] | None:
        """Сводка переноса для ответа API; у задач до хэша входа её нет."""
        value = (self.pipeline_meta or {}).get("incremental")
        return value if isinstance(value, dict) else None

    @property
    def run_stats_view(self) -> dict[str, Any] | None:
        value = (self.pipeline_meta or {}).get("run_stats")
        return value if isinstance(value, dict) else None

    document: Mapped["Document"] = relationship(
        back_populates="analysis_jobs", foreign_keys=[document_id]
    )
    suggestions: Mapped[list["Suggestion"]] = relationship(
        back_populates="analysis_job", cascade="all, delete-orphan"
    )
