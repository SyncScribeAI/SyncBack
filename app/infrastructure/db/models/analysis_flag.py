"""Флаг «проверьте вручную» из ответа ML-конвейера 2.0.

Флаг — не правка: заменить нечего, человеку нужно самому сверить место документа с
цитатами источников. Поэтому отдельная таблица, а не строка suggestions без new_text:
флаг не участвует в review-цикле, экспорте и счётчиках правок.
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base

FLAG_REASON_MAX = 64


class AnalysisFlag(Base):
    __tablename__ = "analysis_flags"
    __table_args__ = (
        # Строка с CHECK, а не enum PostgreSQL: новое значение не потребует ALTER TYPE.
        CheckConstraint("status IN ('pending', 'reviewed')", name="ck_analysis_flags_status"),
        # Редактор читает все флаги текущего анализа в порядке ответа конвейера.
        Index("ix_analysis_flags_job_position", "analysis_job_id", "position"),
        # Без индекса каскадное удаление документа сканировало бы всю таблицу.
        Index("ix_analysis_flags_document_id", "document_id"),
        Index(
            "ix_analysis_flags_decided_by",
            "decided_by",
            postgresql_where=sa.text("decided_by IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    analysis_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Денормализован, как у suggestions: проверка «флаг этого документа» без JOIN.
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Ссылка на блок документа; внешнего ключа нет по той же причине, что у suggestions.block_id.
    block_id: Mapped[str] = mapped_column(String(255), nullable=False)
    section_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    reason: Mapped[str] = mapped_column(String(FLAG_REASON_MAX), nullable=False)
    # Текст блока на момент анализа: подсветка не зависит от того, что вернул конвейер.
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"source_name": ..., "text": ...}] — читается только целиком, вместе с флагом.
    quotes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
