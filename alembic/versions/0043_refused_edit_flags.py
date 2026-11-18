"""Флаги отказанных правок: происхождение флага и предложенная замена.

origin различает флаг из ответа анализа и правку, которую бэкенд не смог однозначно
привязать к месту блока. Прежние строки получают analysis через server_default, поэтому
миграция не переписывает таблицу построчно.

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_flags",
        sa.Column("origin", sa.String(16), nullable=False, server_default="analysis"),
    )
    op.add_column(
        "analysis_flags",
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_analysis_flags_origin",
        "analysis_flags",
        "origin IN ('analysis', 'refused_edit')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analysis_flags_origin", "analysis_flags", type_="check")
    op.drop_column("analysis_flags", "proposal")
    op.drop_column("analysis_flags", "origin")
