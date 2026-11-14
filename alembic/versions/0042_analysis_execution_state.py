"""Состояние вычисления задачи анализа отдельно от её статуса.

status=cancelled фиксирует решение пользователя, а execution_state показывает,
остановил ли воркер работу. Колонки без значения по умолчанию: у прежних задач
состояние неизвестно и выводится из status.

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_jobs", sa.Column("execution_state", sa.String(20), nullable=True))
    op.add_column(
        "analysis_jobs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "analysis_jobs",
        sa.Column("execution_stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_analysis_jobs_execution_state",
        "analysis_jobs",
        "execution_state IN ('not_started', 'running', 'cancel_requested', 'stopped', "
        "'completed', 'unknown')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analysis_jobs_execution_state", "analysis_jobs", type_="check")
    op.drop_column("analysis_jobs", "execution_stopped_at")
    op.drop_column("analysis_jobs", "cancel_requested_at")
    op.drop_column("analysis_jobs", "execution_state")
