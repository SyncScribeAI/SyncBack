"""Снимки источников (source_snapshots) и их участие в задачах (analysis_job_source_snapshots).

Текст снимка и отпечатки сегментов лежат в хранилище по ключу от sha256 текста; в БД
только метаданные. analysis_job_sources уже занята таблицей идемпотентности прежнего
конвейера, поэтому строки задачи живут в analysis_job_source_snapshots.

Удаление снимка, на который ссылается задача, запрещено (RESTRICT): снимки чистит
сервис, когда на них не осталось ссылок. Удаление источника оставляет историю задачи
читаемой (SET NULL).

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("segments", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("project_id", "content_hash", name="uq_source_snapshots_project_hash"),
    )
    op.create_table(
        "analysis_job_source_snapshots",
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_snapshots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("file_storage_key", sa.String(512), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.PrimaryKeyConstraint("job_id", "position", name="pk_analysis_job_source_snapshots"),
        sa.CheckConstraint(
            "status IN ('fetched', 'failed')", name="ck_analysis_job_source_snapshots_status"
        ),
        sa.CheckConstraint(
            "(status = 'fetched') = (snapshot_id IS NOT NULL)",
            name="ck_analysis_job_source_snapshots_snapshot",
        ),
    )
    op.create_index(
        "ix_analysis_job_source_snapshots_snapshot_id",
        "analysis_job_source_snapshots",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_job_source_snapshots_snapshot_id", table_name="analysis_job_source_snapshots"
    )
    op.drop_table("analysis_job_source_snapshots")
    op.drop_table("source_snapshots")
