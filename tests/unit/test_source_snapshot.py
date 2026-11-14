"""Сегменты и отпечатки снимков источников."""

from __future__ import annotations

import uuid

from app.domain.source_snapshot import (
    SegmentKind,
    SnapshotDraft,
    content_hash,
    fingerprint,
    fingerprints_storage_key,
    iter_segments,
    pack_fingerprints,
    snapshot_storage_key,
    unpack_fingerprints,
)

TEXT = (
    "# Лимиты\n\n"
    "Загрузка до 50 МБ.\nВторая строка абзаца.\n\n"
    "- первый пункт\n  продолжение пункта\n- второй пункт\n\n"
    "| Формат | Лимит |\n| PDF | 50 МБ |\n\n"
    "```\nкод\n\nвнутри\n```\n\n"
    "Хвост"
)


def _segments(text: str) -> list[tuple[SegmentKind, str]]:
    return [(s.kind, s.text_in(text)) for s in iter_segments(text)]


def test_segments_follow_unified_source_blocks() -> None:
    assert _segments(TEXT) == [
        (SegmentKind.HEADING, "# Лимиты"),
        (SegmentKind.PARAGRAPH, "Загрузка до 50 МБ.\nВторая строка абзаца."),
        (SegmentKind.LIST_ITEM, "- первый пункт\n  продолжение пункта"),
        (SegmentKind.LIST_ITEM, "- второй пункт"),
        (SegmentKind.TABLE_ROW, "| Формат | Лимит |"),
        (SegmentKind.TABLE_ROW, "| PDF | 50 МБ |"),
        (SegmentKind.CODE, "```\nкод\n\nвнутри\n```"),
        (SegmentKind.PARAGRAPH, "Хвост"),
    ]


def test_segment_indexes_are_sequential_and_offsets_are_codepoints() -> None:
    text = "ёж  \n\nкот"
    segments = list(iter_segments(text))
    assert [s.index for s in segments] == [0, 1]
    assert [(s.start, s.end) for s in segments] == [(0, 2), (6, 9)]


def test_empty_text_has_no_segments() -> None:
    assert list(iter_segments("")) == []
    assert list(iter_segments("\n\n  \n")) == []


def test_heading_closes_paragraph_without_blank_line() -> None:
    assert _segments("абзац\n## Заголовок\nтекст") == [
        (SegmentKind.PARAGRAPH, "абзац"),
        (SegmentKind.HEADING, "## Заголовок"),
        (SegmentKind.PARAGRAPH, "текст"),
    ]


def test_fingerprint_ignores_whitespace_typography_and_markup() -> None:
    base = fingerprint('Лимит "50 МБ" - для PDF')
    assert fingerprint("- Лимит  «50 МБ» — для PDF") == base
    assert fingerprint("Лимит\n“50 МБ” – для PDF") == base


def test_fingerprint_keeps_numbers_and_case() -> None:
    assert fingerprint("Лимит 50 МБ") != fingerprint("Лимит 100 МБ")
    assert fingerprint("лимит 50 МБ") != fingerprint("Лимит 50 МБ")


def test_table_row_fingerprint_ignores_pipe_spacing() -> None:
    assert fingerprint("| PDF | 50 МБ |") == fingerprint("|PDF|50 МБ|")


def test_fingerprints_roundtrip_through_storage_format() -> None:
    values = [0, 1, 2**64 - 1, fingerprint("x")]
    raw = pack_fingerprints(values)
    assert len(raw) == 8 * len(values)
    assert list(unpack_fingerprints(raw)) == values


def test_draft_is_content_addressed_per_project() -> None:
    project = uuid.uuid4()
    draft = SnapshotDraft.of(project, TEXT)
    assert draft.content_hash == content_hash(TEXT)
    assert draft.storage_key == snapshot_storage_key(project, draft.content_hash)
    assert draft.storage_key.endswith(".txt")
    assert fingerprints_storage_key(draft.storage_key).endswith(f"{draft.content_hash}.fp")
    assert (draft.chars, draft.segments) == (len(TEXT), 8)
    assert SnapshotDraft.of(project, TEXT) == draft
