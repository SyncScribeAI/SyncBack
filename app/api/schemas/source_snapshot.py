"""Схемы снимков источников."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class SourceSnapshotContentResponse(BaseModel):
    """Текст снимка; offsets в основаниях правок считаются в кодпойнтах этого текста."""

    id: uuid.UUID
    text: str
    chars: int
    taken_at: datetime | None = None
