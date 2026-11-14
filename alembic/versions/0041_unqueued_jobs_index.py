"""Частичный индекс для сверки задач анализа, потерянных до отправки в брокер.

Сверка раз в минуту ищет pending и dispatched задачи без celery_task_id. Без индекса
каждый такой запрос читал бы всю таблицу задач; частичный индекс содержит только
застрявшие задачи и поэтому почти пуст.

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_analysis_jobs_unqueued",
        "analysis_jobs",
        ["created_at"],
        postgresql_where=sa.text("celery_task_id IS NULL AND status IN ('pending', 'dispatched')"),
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_jobs_unqueued", table_name="analysis_jobs")
