"""Снимки источников.

GET /projects/{id}/source-snapshots/{snapshot_id}/content — текст снимка для просмотра
основания правки.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_allowed_project
from app.api.schemas.source_snapshot import SourceSnapshotContentResponse
from app.core.dependencies import get_source_snapshot_service
from app.domain.exceptions import SourceSnapshotNotFoundError
from app.domain.services.source_snapshot_service import SourceSnapshotService
from app.infrastructure.db.models.project import Project

router = APIRouter(prefix="/projects/{project_id}/source-snapshots", tags=["sources"])


@router.get("/{snapshot_id}/content", response_model=SourceSnapshotContentResponse)
async def get_snapshot_content(
    snapshot_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    service: SourceSnapshotService = Depends(get_source_snapshot_service),
) -> SourceSnapshotContentResponse:
    try:
        content = await service.get_content(project.id, snapshot_id)
    except SourceSnapshotNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SourceSnapshotContentResponse(
        id=content.snapshot.id,
        text=content.text,
        chars=content.snapshot.chars,
        taken_at=content.snapshot.created_at,
    )
