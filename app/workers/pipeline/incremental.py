"""Вход задачи анализа для решения о переносе: хэш, объём и изменения источников.

Модуль собирает данные, а решение принимает домен (app.domain.analysis_reuse). Отпечатки
прошлого входа читаются из .fp-файлов снимков; снимки с тем же хэшем, что у текущего
входа, не скачиваются: их отпечатки уже в памяти.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from itertools import chain

from celery.exceptions import SoftTimeLimitExceeded

from app.domain.analysis_reuse import DeltaSummary, RunStats
from app.domain.interfaces.file_storage import FileStorage
from app.domain.services.source_differ import diff_fingerprints
from app.domain.source_snapshot import (
    JobSourceSnapshot,
    SnapshotDraft,
    SourceSegment,
    fingerprints_storage_key,
    unpack_fingerprints,
)

logger = logging.getLogger("syncscribe.workers.analysis_v2")

Segments = Sequence[tuple[SourceSegment, int]]


def full_run_stats(
    segments: Sequence[Segments], snapshots: Sequence[SnapshotDraft], blocks: int, checked: int
) -> RunStats:
    """Полный прогон: в ML уходят все сегменты и все символы источников."""
    segments_total = sum(len(item) for item in segments)
    chars_total = sum(snapshot.chars for snapshot in snapshots)
    return RunStats(
        segments_total=segments_total,
        segments_sent=segments_total,
        chars_total=chars_total,
        chars_sent=chars_total,
        blocks_total=blocks,
        blocks_checked=checked,
    )


def reused_run_stats(
    segments: Sequence[Segments], snapshots: Sequence[SnapshotDraft], blocks: int
) -> RunStats:
    return RunStats(
        segments_total=sum(len(item) for item in segments),
        segments_sent=0,
        chars_total=sum(snapshot.chars for snapshot in snapshots),
        chars_sent=0,
        blocks_total=blocks,
        blocks_checked=0,
    )


async def delta_against(
    storage: FileStorage,
    baseline_sources: Sequence[JobSourceSnapshot],
    snapshots: Sequence[SnapshotDraft],
    segments: Sequence[Segments],
    job_id: uuid.UUID,
) -> DeltaSummary | None:
    """Изменения входа относительно прошлой задачи; None, если отпечатки не прочитались.

    Сбой здесь не влияет на анализ: это измерение для выборочной отправки, а не её часть.
    """
    current = {snapshot.content_hash: snapshot for snapshot in snapshots}
    base: set[int] = set()
    try:
        for source in baseline_sources:
            snapshot = source.snapshot
            if snapshot is None:
                continue
            same = current.get(snapshot.content_hash)
            if same is not None:
                base.update(same.fingerprints)
                continue
            raw = await storage.download(fingerprints_storage_key(snapshot.storage_key))
            base.update(unpack_fingerprints(raw))
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning(
            "Анализ v2: отпечатки прошлого входа не прочитаны",
            exc_info=True,
            extra={"job_id": str(job_id)},
        )
        return None
    delta = diff_fingerprints(frozenset(base), chain.from_iterable(segments))
    return DeltaSummary(
        changed_share=delta.changed_share,
        segments_added=len(delta.added),
        segments_removed=len(delta.removed_fingerprints),
    )
