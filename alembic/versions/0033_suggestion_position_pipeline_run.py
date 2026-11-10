"""Порядок правок и сводка вызова ML-конвейера.

suggestions.position — номер правки в ответе конвейера; индекс порядка выдачи заменяет
ix_suggestions_job_created_at_id. analysis_jobs: request_id, pipeline_meta,
checked_blocks, failed_blocks — заполняются задачей анализа v2.

Существующие правки получают position = 0 и сохраняют прежний порядок (created_at, id).

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default задаёт значение без перезаписи таблицы (PostgreSQL 11+).
    op.add_column(
        "suggestions",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_suggestions_job_order",
        "suggestions",
        ["analysis_job_id", "created_at", "position", "id"],
        unique=False,
    )
    op.drop_index("ix_suggestions_job_created_at_id", table_name="suggestions")

    op.add_column("analysis_jobs", sa.Column("request_id", sa.String(length=128), nullable=True))
    op.add_column(
        "analysis_jobs",
        sa.Column("pipeline_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("analysis_jobs", sa.Column("checked_blocks", sa.Integer(), nullable=True))
    op.add_column("analysis_jobs", sa.Column("failed_blocks", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_jobs", "failed_blocks")
    op.drop_column("analysis_jobs", "checked_blocks")
    op.drop_column("analysis_jobs", "pipeline_meta")
    op.drop_column("analysis_jobs", "request_id")

    op.create_index(
        "ix_suggestions_job_created_at_id",
        "suggestions",
        ["analysis_job_id", "created_at", "id"],
        unique=False,
    )
    op.drop_index("ix_suggestions_job_order", table_name="suggestions")
    op.drop_column("suggestions", "position")
