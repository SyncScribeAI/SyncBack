import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, Index, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.models.enums import AuditAction

_values = lambda e: [m.value for m in e]  # noqa: E731

# Действия над документом целиком, без правки. Решения по флагам — тоже здесь:
# у флага нет строки в suggestions, его id пишется в details.flag_id.
DOCUMENT_ACTIONS_SQL = "'download', 'finalize', 'finalize_review', 'flag_review', 'flag_reopen'"


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        # Хотя бы одна из двух ссылок должна быть заполнена.
        sa.CheckConstraint(
            "suggestion_id IS NOT NULL OR document_id IS NOT NULL",
            name="ck_audit_logs_ref_not_null",
        ),
        # Действия над документом не ссылаются на правку, остальные — ссылаются.
        sa.CheckConstraint(
            f"(action IN ({DOCUMENT_ACTIONS_SQL}) "
            "AND document_id IS NOT NULL AND suggestion_id IS NULL) OR "
            f"(action NOT IN ({DOCUMENT_ACTIONS_SQL}) "
            "AND suggestion_id IS NOT NULL)",
            name="ck_audit_logs_target",
        ),
        # Покрывает и запросы только по document_id, отдельный индекс не нужен.
        Index("ix_audit_logs_document_created", "document_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("suggestions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,  # остаётся: запросы только по suggestion_id без created_at
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    # SET NULL: удаление пользователя НЕ должно уничтожать аудит-лог.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        sa.Enum(AuditAction, name="audit_action", values_callable=_values),
        nullable=False,
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
