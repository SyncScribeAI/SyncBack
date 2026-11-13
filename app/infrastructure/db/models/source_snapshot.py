"""Снимки источников и их участие в задачах анализа.

source_snapshots — адресуемый по содержимому текст источника в едином формате; сам текст
и отпечатки сегментов лежат в хранилище, в БД только метаданные. Один текст в проекте
хранится один раз, поэтому (project_id, content_hash) уникален.

analysis_job_source_snapshots — какие источники и в каком виде ушли в конкретную задачу.
Имя analysis_job_sources уже занято таблицей идемпотентности прежнего конвейера, поэтому
у новой таблицы другое имя.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base


class SourceSnapshotModel(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "content_hash", name="uq_source_snapshots_project_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    chars: Mapped[int] = mapped_column(Integer, nullable=False)
    segments: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalysisJobSourceSnapshot(Base):
    __tablename__ = "analysis_job_source_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('fetched', 'failed')", name="ck_analysis_job_source_snapshots_status"
        ),
        CheckConstraint(
            "(status = 'fetched') = (snapshot_id IS NOT NULL)",
            name="ck_analysis_job_source_snapshots_snapshot",
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    # Источник могут удалить, а история задачи должна остаться читаемой.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    file_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)

    snapshot: Mapped[SourceSnapshotModel | None] = relationship(lazy="joined")
