"""Экспорт правок с границами: каждое из повторов фразы меняется в своём месте.

Смещения берутся у настоящего парсера, как их получает задача анализа: так тест ловит
расхождение между plain_text парсера и текстом, который видит экспортёр.
"""

import io
import time

import pytest
from docx import Document as DocxDocument

from app.domain.interfaces.document_exporter import AppliedChange
from app.infrastructure.exporters.docx_exporter import DocxExporter
from app.infrastructure.exporters.text_edits import TextEdit, apply_edits
from app.infrastructure.exporters.text_exporter import TextExporter
from app.infrastructure.parsers.docx_parser import DocxParser

MD = "Ответ — 10 дней.\nПовтор — 10 дней.\n\nВозврат — 10 дней.\n"
PHRASE = "10 дней"


def _occurrences(text: str, phrase: str = PHRASE) -> list[int]:
    found, index = [], text.find(phrase)
    while index >= 0:
        found.append(index)
        index = text.find(phrase, index + 1)
    return found


def _at(
    start: int,
    change_type: str = "modify",
    new_text: str | None = "5 дней",
    old_text: str = PHRASE,
) -> AppliedChange:
    return AppliedChange(
        section_ref="",
        change_type=change_type,
        old_text=old_text,
        new_text=new_text,
        start_offset=start,
        end_offset=start + len(old_text),
    )


def _text(changes: list[AppliedChange], text: str = MD) -> str:
    return TextExporter().apply_changes(text.encode(), changes).decode()


# ── txt / markdown ────────────────────────────────────────────────────────────────


def test_text_changes_each_occurrence_by_offsets() -> None:
    first, second, third = _occurrences(MD)
    result = _text([_at(third, new_text="3 дня"), _at(second)])
    assert result == "Ответ — 10 дней.\nПовтор — 5 дней.\n\nВозврат — 3 дня.\n"
    assert first == MD.find(PHRASE)


def test_text_falls_back_to_first_occurrence_when_offsets_disagree() -> None:
    # Файл заменили после анализа: под границами другой текст — ищем, как раньше.
    stale = _at(len(MD) - 3)
    assert _text([stale]) == MD.replace(PHRASE, "5 дней", 1)


def test_text_delete_and_overlap_by_offsets() -> None:
    first, second, _ = _occurrences(MD)
    result = _text(
        [
            _at(second, "delete", None),
            # Начинается внутри уже применённой правки — пропускается.
            _at(second + 1, old_text="0 дней"),
            _at(first),
        ]
    )
    assert result == "Ответ — 5 дней.\nПовтор — .\n\nВозврат — 10 дней.\n"


def test_text_addition_goes_after_line_of_its_own_anchor() -> None:
    _, second, _ = _occurrences(MD)
    result = _text([_at(second, "add", "Можно продлить.")])
    assert result == (
        "Ответ — 10 дней.\nПовтор — 10 дней.\n\nМожно продлить.\n\nВозврат — 10 дней.\n"
    )


def test_text_additions_after_one_anchor_keep_order_and_end_of_file() -> None:
    *_, third = _occurrences(MD)
    result = _text([_at(third, "add", "Первое."), _at(third, "add", "Второе.")])
    assert result.endswith("Возврат — 10 дней.\n\nПервое.\n\nВторое.\n")


def test_text_mixes_anchored_and_legacy_changes() -> None:
    *_, third = _occurrences(MD)
    legacy = AppliedChange(section_ref="", change_type="modify", old_text="Ответ", new_text="Ок")
    assert _text([_at(third), legacy]) == (
        "Ок — 10 дней.\nПовтор — 10 дней.\n\nВозврат — 5 дней.\n"
    )


def test_apply_edits_without_edits_returns_text() -> None:
    assert apply_edits("abc", []) == "abc"
    assert apply_edits("abc", [TextEdit(0, 0, block="X")]) == "X\n\nabc"


# ── docx ─────────────────────────────────────────────────────────────────────────


def _docx(*paragraphs: str, table_cell: str | None = None) -> bytes:
    document = DocxDocument()
    for text in paragraphs:
        document.add_paragraph(text)
    if table_cell is not None:
        document.add_table(rows=1, cols=1).cell(0, 0).text = table_cell
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraphs(raw: bytes) -> list[str]:
    document = DocxDocument(io.BytesIO(raw))
    texts = [p.text for p in document.paragraphs]
    texts.extend(cell.text for table in document.tables for cell in table._cells)
    return texts


def test_docx_changes_occurrences_in_their_paragraphs() -> None:
    raw = _docx("Ответ — 10 дней, повтор — 10 дней.", "Возврат — 10 дней.")
    plain = DocxParser().parse(raw).plain_text
    first, second, third = _occurrences(plain)
    result = DocxExporter().apply_changes(
        raw, [_at(second, new_text="7 дней"), _at(third), _at(first, "delete", None)]
    )
    assert _paragraphs(result) == ["Ответ — , повтор — 7 дней.", "Возврат — 5 дней."]


def test_docx_offsets_reach_table_cells_in_parser_order() -> None:
    raw = _docx("Ответ — 10 дней.", table_cell="Срок — 10 дней")
    plain = DocxParser().parse(raw).plain_text
    *_, in_cell = _occurrences(plain)
    result = DocxExporter().apply_changes(raw, [_at(in_cell)])
    assert _paragraphs(result) == ["Ответ — 10 дней.", "Срок — 5 дней"]


def test_docx_additions_follow_their_paragraph_in_order() -> None:
    raw = _docx("Ответ — 10 дней.", "Возврат — 10 дней.", "Конец.")
    plain = DocxParser().parse(raw).plain_text
    _, second = _occurrences(plain)
    result = DocxExporter().apply_changes(
        raw, [_at(second, "add", "Первое."), _at(second, "add", "Второе.")]
    )
    assert _paragraphs(result) == [
        "Ответ — 10 дней.",
        "Возврат — 10 дней.",
        "Первое.",
        "Второе.",
        "Конец.",
    ]


def test_docx_falls_back_when_offsets_point_elsewhere() -> None:
    raw = _docx("Ответ — 10 дней.", "Возврат — 10 дней.")
    result = DocxExporter().apply_changes(raw, [_at(2)])
    assert _paragraphs(result) == ["Ответ — 5 дней.", "Возврат — 10 дней."]


# ── масштаб ──────────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_text_export_by_offsets_scales_linearly() -> None:
    def run(scale: int) -> float:
        text = "".join(f"Пункт {i}: срок 10 дней.\n\n" for i in range(2000 * scale))
        starts = _occurrences(text)[: 500 * scale]
        changes = [_at(start) for start in starts]
        raw = text.encode()
        began = time.perf_counter()
        result = TextExporter().apply_changes(raw, changes)
        elapsed = time.perf_counter() - began
        assert result.decode().count("5 дней") == 500 * scale
        return elapsed

    small = min(run(1) for _ in range(3))
    large = min(run(4) for _ in range(3))
    # Последовательные replace дали бы ×16; запас на шум CI.
    assert large < small * 8
