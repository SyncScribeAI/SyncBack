"""Флаги «проверьте вручную» (analysis_flags) и их события в аудите.

analysis_flags — места документа, которые конвейер ML 2.0 просит проверить человеку:
правки нет, есть причина, обоснование и цитаты источников. Статус — строка с CHECK
(pending | reviewed), как suggestions.origin.

Решения по флагам пишутся в audit_logs действиями flag_review / flag_reopen без
suggestion_id (id флага — в details.flag_id), поэтому они добавлены в список действий над
документом в ck_audit_logs_target.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_audit_logs_target"
NEW_ENUM_VALUES = ("flag_review", "flag_reopen")

NEW_CHECK = (
    "(action IN ('download', 'finalize', 'finalize_review', 'flag_review', 'flag_reopen') "
    "AND document_id IS NOT NULL AND suggestion_id IS NULL) OR "
    "(action NOT IN ('download', 'finalize', 'finalize_review', 'flag_review', 'flag_reopen') "
    "AND suggestion_id IS NOT NULL)"
)

OLD_CHECK = (
    "(action IN ('download', 'finalize', 'finalize_review') "
    "AND document_id IS NOT NULL AND suggestion_id IS NULL) OR "
    "(action NOT IN ('download', 'finalize', 'finalize_review') "
    "AND suggestion_id IS NOT NULL)"
)


def upgrade() -> None:
    # Новое значение enum нельзя использовать в той же транзакции, где оно добавлено,
    # а ограничение ниже на него ссылается.
    with op.get_context().autocommit_block():
        for value in NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")
    op.drop_constraint(CHECK_NAME, "audit_logs", type_="check")
    op.create_check_constraint(CHECK_NAME, "audit_logs", NEW_CHECK)

    op.create_table(
        "analysis_flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_id", sa.String(length=255), nullable=False),
        sa.Column("section_ref", sa.String(length=500), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("quotes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('pending', 'reviewed')", name="ck_analysis_flags_status"),
        sa.ForeignKeyConstraint(["analysis_job_id"], ["analysis_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_flags_job_position", "analysis_flags", ["analysis_job_id", "position"]
    )
    op.create_index("ix_analysis_flags_document_id", "analysis_flags", ["document_id"])
    op.create_index(
        "ix_analysis_flags_decided_by",
        "analysis_flags",
        ["decided_by"],
        postgresql_where=sa.text("decided_by IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_flags_decided_by", table_name="analysis_flags")
    op.drop_index("ix_analysis_flags_document_id", table_name="analysis_flags")
    op.drop_index("ix_analysis_flags_job_position", table_name="analysis_flags")
    op.drop_table("analysis_flags")
    op.drop_constraint(CHECK_NAME, "audit_logs", type_="check")
    # События флагов старому правилу не удовлетворяют, поэтому ограничение возвращается
    # без проверки существующих строк, как в 0026. Значения enum PostgreSQL удалять не
    # умеет, они остаются.
    op.execute(f"ALTER TABLE audit_logs ADD CONSTRAINT {CHECK_NAME} CHECK ({OLD_CHECK}) NOT VALID")
