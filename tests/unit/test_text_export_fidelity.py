"""Экспорт txt и md: всё вне затронутых правками строк остаётся байт в байт исходным."""

from __future__ import annotations

import random

import pytest

from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.text_normalization import normalize_text
from app.infrastructure.exporters.text_edits import TextEdit, apply_edits
from app.infrastructure.exporters.text_exporter import TextExporter

FIXTURES = {
    "bom": "\ufeffПервая строка\nВторая\n".encode(),
    "crlf": "Раз\r\nДва\r\n\r\nТри\r\n".encode(),
    "mixed": "Раз\r\nДва\nТри\r\n".encode(),
    "nbsp": "50\u00a0МБ и\u202fещё\n".encode(),
    "no_trailing_newline": "Без перевода строки".encode(),
    "invalid_utf8": b"\xff\xfe broken \xc3\n\xd0\xbf\xd1\x80\xd0\xb8\n",
    "decomposed": "е\u0308ж\nтекст\n".encode(),
    "lone_cr": b"a\rb\nc\n",
}


def _modify(text: str, old: str, new: str, *, occurrence: int = 0) -> AppliedChange:
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(old, start + 1)
    return AppliedChange(
        section_ref="",
        change_type="modify",
        old_text=old,
        new_text=new,
        start_offset=start,
        end_offset=start + len(old),
    )


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_export_without_changes_is_byte_identical(name: str) -> None:
    raw = FIXTURES[name]
    assert TextExporter().apply_changes(raw, []) == raw


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_unapplicable_change_keeps_bytes(name: str) -> None:
    raw = FIXTURES[name]
    change = AppliedChange(
        section_ref="", change_type="modify", old_text="нет такого", new_text="x"
    )
    assert TextExporter().apply_changes(raw, [change]) == raw


def test_one_change_touches_only_its_line_and_keeps_crlf_and_bom() -> None:
    raw = "\ufeffЗаголовок\r\nЛимит 50\u00a0МБ\r\nСрок 10 дней\r\nХвост\u00a0x".encode()
    text = normalize_text(raw.decode())
    result = TextExporter().apply_changes(raw, [_modify(text, "10 дней", "5 дней")])
    assert result == "\ufeffЗаголовок\r\nЛимит 50\u00a0МБ\r\nСрок 5 дней\r\nХвост\u00a0x".encode()


def test_change_in_first_line_keeps_bom() -> None:
    raw = "\ufeffСрок 10 дней\nдальше\n".encode()
    text = normalize_text(raw.decode())
    result = TextExporter().apply_changes(raw, [_modify(text, "10", "5")])
    assert result == "\ufeffСрок 5 дней\nдальше\n".encode()


def test_multiline_change_uses_region_line_endings() -> None:
    raw = b"one\r\ntwo\r\nthree\r\n"
    text = normalize_text(raw.decode())
    result = TextExporter().apply_changes(raw, [_modify(text, "one\ntwo", "uno\ndos")])
    assert result == b"uno\r\ndos\r\nthree\r\n"


def test_repeated_phrase_changes_only_the_addressed_occurrence() -> None:
    raw = "Срок 10 дней\nи снова Срок 10 дней\n".encode()
    text = normalize_text(raw.decode())
    result = TextExporter().apply_changes(raw, [_modify(text, "10", "5", occurrence=1)])
    assert result == "Срок 10 дней\nи снова Срок 5 дней\n".encode()


def test_unanchored_change_is_found_by_first_occurrence() -> None:
    raw = "Лимит\u00a050 МБ\r\nещё\r\n".encode()
    change = AppliedChange(
        section_ref="", change_type="modify", old_text="Лимит 50 МБ", new_text="Лимит 100 МБ"
    )
    assert TextExporter().apply_changes(raw, [change]) == "Лимит 100 МБ\r\nещё\r\n".encode()


def test_addition_after_crlf_line_uses_crlf() -> None:
    raw = b"# A\r\nanchor\r\nnext\r\n"
    change = AppliedChange(section_ref="", change_type="add", old_text="anchor", new_text="new")
    assert (
        TextExporter().apply_changes(raw, [change]) == b"# A\r\nanchor\r\n\r\nnew\r\n\r\nnext\r\n"
    )


def test_unchanged_lines_compared_line_by_line() -> None:
    lines = [f"строка {i}\u00a0x" for i in range(50)]
    raw = "\r\n".join(lines).encode() + b"\r\n"
    text = normalize_text(raw.decode())
    result = TextExporter().apply_changes(raw, [_modify(text, "строка 17", "строка семнадцать")])
    before, after = raw.split(b"\r\n"), result.split(b"\r\n")
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert differing == [17]


def test_matches_whole_text_application_after_normalization() -> None:
    """Построчный экспорт после нормализации совпадает с правками по целому тексту."""
    rng = random.Random(7)
    alphabet = ["a", "б", " ", "\u00a0", "е\u0308", "\r\n", "\n", "x", "\ufeff", "\u200b"]
    for _ in range(300):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60))).encode()
        text = normalize_text(raw.decode())
        if not text.strip():
            continue
        changes: list[AppliedChange] = []
        edits: list[TextEdit] = []
        cursor = 0
        while cursor < len(text) and len(changes) < 3:
            start = rng.randint(cursor, len(text) - 1)
            end = min(len(text), start + rng.randint(1, 5))
            old = text[start:end]
            if old.strip():
                new = rng.choice(["", "Z", "новое\nзначение"])
                changes.append(
                    AppliedChange(
                        section_ref="",
                        change_type="modify",
                        old_text=old,
                        new_text=new,
                        start_offset=start,
                        end_offset=end,
                    )
                )
                edits.append(TextEdit(start, end, replacement=new))
            cursor = end
        if not changes:
            continue
        result = TextExporter().apply_changes(raw, changes).decode("utf-8")
        assert normalize_text(result) == apply_edits(text, edits)
