"""Differ сегментов источника по отпечаткам прошлого входа."""

from __future__ import annotations

from app.domain.services.source_differ import diff_fingerprints
from app.domain.source_snapshot import segment_fingerprints

BASE_TEXT = "Первый абзац.\n\nВторой абзац.\n\nТретий абзац."


def _base() -> frozenset[int]:
    return frozenset(fp for _, fp in segment_fingerprints(BASE_TEXT))


def test_same_text_has_no_changes() -> None:
    delta = diff_fingerprints(_base(), segment_fingerprints(BASE_TEXT))
    assert (len(delta.unchanged), delta.added, delta.removed_fingerprints) == (3, (), frozenset())
    assert delta.changed_share == 0.0


def test_inserted_segment_is_added() -> None:
    text = "Первый абзац.\n\nНовый.\n\nВторой абзац.\n\nТретий абзац."
    delta = diff_fingerprints(_base(), segment_fingerprints(text))
    [added] = delta.added
    assert added.text_in(text) == "Новый."
    assert delta.removed_fingerprints == frozenset()
    assert delta.changed_share == len("Новый.") / (13 + 6 + 13 + 13)


def test_deleted_segment_is_removed() -> None:
    text = "Первый абзац.\n\nТретий абзац."
    delta = diff_fingerprints(_base(), segment_fingerprints(text))
    assert delta.added == ()
    [removed] = delta.removed_fingerprints
    assert (
        removed
        == dict((s.text_in(BASE_TEXT), fp) for s, fp in segment_fingerprints(BASE_TEXT))[
            "Второй абзац."
        ]
    )


def test_reordered_segments_are_unchanged() -> None:
    text = "Третий абзац.\n\nПервый абзац.\n\nВторой абзац."
    delta = diff_fingerprints(_base(), segment_fingerprints(text))
    assert (len(delta.unchanged), delta.added, delta.changed_share) == (3, (), 0.0)


def test_edited_segment_is_both_added_and_removed() -> None:
    text = "Первый абзац.\n\nВторой абзац, исправленный.\n\nТретий абзац."
    delta = diff_fingerprints(_base(), segment_fingerprints(text))
    assert [s.text_in(text) for s in delta.added] == ["Второй абзац, исправленный."]
    assert len(delta.removed_fingerprints) == 1


def test_markup_and_typography_do_not_count_as_changes() -> None:
    text = "- Первый абзац.\n\n## Второй   абзац.\n\nТретий абзац."
    delta = diff_fingerprints(_base(), segment_fingerprints(text))
    assert delta.added == ()


def test_empty_input_has_zero_share() -> None:
    delta = diff_fingerprints(_base(), [])
    assert (delta.segments_total, delta.changed_share, len(delta.removed_fingerprints)) == (
        0,
        0.0,
        3,
    )
