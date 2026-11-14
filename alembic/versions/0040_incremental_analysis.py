"""Инкрементальный анализ: связь задачи с базовой и устаревшие решения.

baseline_job_id указывает задачу, результат которой перенесён в эту целиком или для
непроверенных блоков; по цепочке таких ссылок считается, сколько выборочных задач
прошло после полного анализа. ON DELETE SET NULL: удаление старой задачи не должно
удалять новую. Частичный индекс держит это удаление дешёвым.

suggestions.stale отмечает перенесённое решение пользователя, основание которого с тех
пор изменилось в источнике. Значение по умолчанию false не требует перезаписи таблицы.

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_jobs",
        sa.Column(
            "baseline_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analysis_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_analysis_jobs_baseline_job_id",
        "analysis_jobs",
        ["baseline_job_id"],
        postgresql_where=sa.text("baseline_job_id IS NOT NULL"),
    )
    op.add_column(
        "suggestions",
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("suggestions", "stale")
    op.drop_index("ix_analysis_jobs_baseline_job_id", table_name="analysis_jobs")
    op.drop_column("analysis_jobs", "baseline_job_id")
