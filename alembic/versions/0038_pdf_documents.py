"""PDF как документ: значение pdf в document_format и geometry у правок и флагов.

geometry хранит прямоугольники на страницах оригинала ({"rects": [[page, x0, y0, x1,
y1], ...]}): правку в PDF нельзя показать вставкой в текст, только поверх страницы.
Считается воркером один раз, чтобы просмотр и экспорт не открывали PDF заново. Поле
nullable: у правок других форматов и прежних анализов геометрии нет.

Значение перечисления при откате остаётся: PostgreSQL не удаляет значения ENUM, а
пересоздание типа ради отката не стоит риска для строк с format = 'pdf'.

Revision ID: 0038
Revises: 0037
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE document_format ADD VALUE IF NOT EXISTS 'pdf'")
    for table in ("suggestions", "analysis_flags"):
        op.add_column(
            table,
            sa.Column("geometry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    for table in ("analysis_flags", "suggestions"):
        op.drop_column(table, "geometry")
