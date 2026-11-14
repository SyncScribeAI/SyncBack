"""Повторное использование результата анализа: хэш входа, стратегия и перенос правок.

input_hash фиксирует вход анализа (версия контракта, провайдер, файл документа и
снимки источников). Совпадение хэша с прошлой полной задачей документа позволяет
перенести её результат без вызова ML. strategy=full отключает перенос для одного
запуска; строка с CHECK, а не enum, как у origin правок.

carried_from_id связывает перенесённую правку или флаг с исходной строкой: по нему
связи правки с источниками копируются одним INSERT ... SELECT, а ревью видит, что
решение пользователя пришло из прошлого анализа. ON DELETE SET NULL: удаление старой
задачи не должно удалять перенесённые строки. Частичные индексы держат это удаление
дешёвым и не занимают места у строк без переноса.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_jobs", sa.Column("input_hash", sa.CHAR(64), nullable=True))
    op.add_column(
        "analysis_jobs",
        sa.Column("strategy", sa.String(8), nullable=False, server_default="auto"),
    )
    op.create_check_constraint(
        "ck_analysis_jobs_strategy", "analysis_jobs", "strategy IN ('auto', 'full')"
    )
    for table in ("suggestions", "analysis_flags"):
        op.add_column(
            table,
            sa.Column(
                "carried_from_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_carried_from_id",
            table,
            ["carried_from_id"],
            postgresql_where=sa.text("carried_from_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("analysis_flags", "suggestions"):
        op.drop_index(f"ix_{table}_carried_from_id", table_name=table)
        op.drop_column(table, "carried_from_id")
    op.drop_constraint("ck_analysis_jobs_strategy", "analysis_jobs", type_="check")
    op.drop_column("analysis_jobs", "strategy")
    op.drop_column("analysis_jobs", "input_hash")
