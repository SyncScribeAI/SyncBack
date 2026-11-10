"""
Регистрация ORM-моделей — импортируется в alembic/env.py и app.core.dependencies
для того, чтобы Base.metadata содержала все таблицы.
"""

from app.infrastructure.db.models.analysis_flag import AnalysisFlag  # noqa: F401
from app.infrastructure.db.models.analysis_job import AnalysisJob  # noqa: F401
from app.infrastructure.db.models.analysis_job_source import AnalysisJobSource  # noqa: F401
from app.infrastructure.db.models.audit_log import AuditLog  # noqa: F401
from app.infrastructure.db.models.dashboard_snapshot import DashboardSnapshot  # noqa: F401
from app.infrastructure.db.models.document import Document  # noqa: F401
from app.infrastructure.db.models.document_block import DocumentBlock  # noqa: F401
from app.infrastructure.db.models.document_open import DocumentOpen  # noqa: F401
from app.infrastructure.db.models.document_source import document_sources  # noqa: F401
from app.infrastructure.db.models.enums import (  # noqa: F401
    AnalysisJobStatus,
    DocumentStatus,
    SuggestionStatus,
)
from app.infrastructure.db.models.project import Project  # noqa: F401
from app.infrastructure.db.models.source import Source  # noqa: F401
from app.infrastructure.db.models.suggestion import Suggestion  # noqa: F401
from app.infrastructure.db.models.suggestion_source import suggestion_sources  # noqa: F401
from app.infrastructure.db.models.user import User  # noqa: F401
