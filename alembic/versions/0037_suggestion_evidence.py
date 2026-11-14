"""Основания правок: suggestions.evidence (JSONB).

Цитаты и их место в снимках источников хранятся рядом с правкой, как цитаты флагов в
analysis_flags.quotes: читаются только вместе с правкой, поиска по ним нет. Поле
nullable, у правок прежних анализов оснований нет.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "suggestions",
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("suggestions", "evidence")
