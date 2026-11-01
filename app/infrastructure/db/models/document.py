"""
Document ORM model.

current_analysis_job_id — ссылка на последний запущенный анализ (ON DELETE SET NULL).
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.models.enums import DocumentFormat, DocumentStatus

if TYPE_CHECKING:
    from app.infrastructure.db.models.analysis_job import AnalysisJob
    from app.infrastructure.db.models.project import Project
    from app.infrastructure.db.models.source import Source


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_documents_size_bytes_non_negative"),
        # Keyset-пагинация списка документов проекта: ORDER BY created_at DESC, id DESC.
        Index(
            "ix_documents_project_created_at_id",
            "project_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # Поиск по подстроке имени (ILIKE '%...%') в «Моих документах»; требует pg_trgm.
        Index(
            "ix_documents_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    format: Mapped[DocumentFormat] = mapped_column(
        Enum(
            DocumentFormat,
            name="document_format",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DocumentStatus.DRAFT,
        server_default=DocumentStatus.DRAFT.value,
    )
    review_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Денормализованная ссылка на текущий (последний запущенный) анализ; ставится
    # при создании задачи в AnalysisJobService.
    current_analysis_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exported_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project: Mapped["Project"] = relationship("Project", back_populates="documents")
    sources: Mapped[list["Source"]] = relationship(
        "Source", secondary="document_sources", back_populates="documents"
    )
    analysis_jobs: Mapped[list["AnalysisJob"]] = relationship(
        "AnalysisJob",
        back_populates="document",
        foreign_keys="AnalysisJob.document_id",
        cascade="all, delete-orphan",
    )
    # Разрывает цикл Document -> current job -> Document при ORM DELETE:
    # SQLAlchemy сначала обнуляет current_analysis_job_id, затем удаляет историю
    # и документ. FK ON DELETE SET NULL сам по себе не упорядочивает ORM flush.
    # Без cascade: удаление этой ссылки не должно удалять задачу анализа.
    current_analysis_job: Mapped["AnalysisJob | None"] = relationship(
        "AnalysisJob",
        foreign_keys=[current_analysis_job_id],
        primaryjoin="Document.current_analysis_job_id == AnalysisJob.id",
        post_update=True,
        lazy="select",
        uselist=False,
    )
