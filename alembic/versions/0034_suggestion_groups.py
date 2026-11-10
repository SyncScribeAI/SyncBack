"""Группы правок одной фразы и происхождение правки.

suggestions.group_key — общий ключ правок, меняющих одну фразу одинаково (NULL у правки
без пары); suggestions.origin — model или propagated (догоняющая правка бэкенда).
Частичный индекс по (analysis_job_id, group_key) обслуживает фильтр ?group_key= и агрегат
групп редактора, не раздуваясь на одиночных правках.

Существующие правки получают origin = model и group_key = NULL.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("suggestions", sa.Column("group_key", sa.String(length=64), nullable=True))
    # server_default задаёт значение без перезаписи таблицы (PostgreSQL 11+).
    op.add_column(
        "suggestions",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="model"),
    )
    op.create_check_constraint(
        "ck_suggestions_origin", "suggestions", "origin IN ('model', 'propagated')"
    )
    op.create_index(
        "ix_suggestions_job_group",
        "suggestions",
        ["analysis_job_id", "group_key"],
        unique=False,
        postgresql_where=sa.text("group_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_suggestions_job_group", table_name="suggestions")
    op.drop_constraint("ck_suggestions_origin", "suggestions", type_="check")
    op.drop_column("suggestions", "origin")
    op.drop_column("suggestions", "group_key")
