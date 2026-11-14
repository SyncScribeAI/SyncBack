"""
Suggestion ORM model.

decided_by и decided_at заполняются при решении по правке и обнуляются при сбросе.
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
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.evidence import present_evidence
from app.infrastructure.db.base import Base
from app.infrastructure.db.models.enums import ChangeType, SuggestionStatus

if TYPE_CHECKING:
    from app.infrastructure.db.models.analysis_job import AnalysisJob
    from app.infrastructure.db.models.source import Source

_values = lambda e: [m.value for m in e]  # noqa: E731


class Suggestion(Base):
    __tablename__ = "suggestions"
    __table_args__ = (
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="ck_suggestions_confidence_range",
        ),
        Index("ix_suggestions_job_status", "analysis_job_id", "status"),
        Index("ix_suggestions_document_status", "document_id", "status"),
        # Порядок выдачи правок задачи: ORDER BY created_at, position, id.
        Index("ix_suggestions_job_order", "analysis_job_id", "created_at", "position", "id"),
        CheckConstraint(
            "origin IN ('model', 'propagated')",
            name="ck_suggestions_origin",
        ),
        # Фильтр ?group_key= и агрегат групп редактора; одиночные правки ключа не имеют.
        Index(
            "ix_suggestions_job_group",
            "analysis_job_id",
            "group_key",
            postgresql_where=sa.text("group_key IS NOT NULL"),
        ),
        Index(
            "ix_suggestions_decided_by",
            "decided_by",
            postgresql_where=sa.text("decided_by IS NOT NULL"),
        ),
        Index(
            "ix_suggestions_carried_from_id",
            "carried_from_id",
            postgresql_where=sa.text("carried_from_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    analysis_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
        # Отдельный индекс не нужен: его покрывает ix_suggestions_job_status.
    )
    # Денормализованный document_id избавляет от JOIN через analysis_jobs;
    # индекс покрывает ix_suggestions_document_status.
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    section_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    # Ссылка на document_blocks.block_ref. Внешний ключ не объявлен: block_ref уникален
    # только в паре с document_id, поэтому ссылка должна быть составной.
    block_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Границы original_text в plain_text документа; NULL — правка не привязана к блоку
    # (прежний конвейер legacy_llm).
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Номер правки в ответе конвейера. Правки одного анализа вставляются одной транзакцией
    # с одинаковым created_at, и без position их порядок задавал бы случайный uuid.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Общий ключ правок одной фразы (suggestion_groups.group_key); NULL — у правки нет пары.
    group_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Строка с CHECK, а не enum PostgreSQL: новое значение не потребует ALTER TYPE.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="model", server_default="model"
    )
    change_type: Mapped[ChangeType] = mapped_column(
        sa.Enum(ChangeType, name="change_type", values_callable=_values),
        nullable=False,
    )
    status: Mapped[SuggestionStatus] = mapped_column(
        sa.Enum(SuggestionStatus, name="suggestion_status", values_callable=_values),
        nullable=False,
        default=SuggestionStatus.PENDING,
        server_default=SuggestionStatus.PENDING.value,
    )
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Текст принятой правки после ручной корректировки. Обнуляется при сбросе решения;
    # при экспорте имеет приоритет над suggested_text.
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Основания правки: цитаты и их место в снимках источников (app.domain.evidence).
    # None у правок, созданных до оснований, и у правок без цитат.
    evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Прямоугольники правки на страницах PDF (app.domain.document_geometry.rects_to_json).
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Строка прошлого анализа, из которой перенесена эта; NULL у результата вызова ML.
    carried_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suggestions.id", ondelete="SET NULL"),
        nullable=True,
    )

    analysis_job: Mapped["AnalysisJob"] = relationship(back_populates="suggestions")
    sources: Mapped[list["Source"]] = relationship(secondary="suggestion_sources", lazy="selectin")

    @property
    def evidence_view(self) -> list[dict[str, Any]]:
        """Основания для ответа API. Источники правки уже загружены selectin, поэтому
        доступность оригинала проверяется без отдельного запроса."""
        keys = {str(source.id): source.storage_key for source in self.sources}
        return present_evidence(self.evidence, keys)
