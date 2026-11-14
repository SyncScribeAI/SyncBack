"""Экспорт docx: замена внутри ранов сохраняет оформление и не трогает другие абзацы."""

from __future__ import annotations

import io
import zipfile

from docx import Document as DocxDocument
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.text.paragraph import Paragraph

from app.domain.interfaces.document_exporter import AppliedChange
from app.infrastructure.exporters.docx_exporter import DocxExporter
from app.infrastructure.parsers.docx_parser import DocxParser


def _save(document: object) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)  # type: ignore[attr-defined]
    return buffer.getvalue()


def _change(raw: bytes, old: str, new: str, *, kind: str = "modify", occurrence: int = 0):
    text = DocxParser().parse(raw).plain_text
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(old, start + 1)
    return AppliedChange(
        section_ref="",
        change_type=kind,
        old_text=old,
        new_text=new,
        start_offset=start,
        end_offset=start + len(old),
    )


def _runs(paragraph: Paragraph) -> list[tuple[str, bool, bool]]:
    return [(r.text, bool(r.bold), bool(r.italic)) for r in paragraph.runs]


def _export(raw: bytes, changes: list[AppliedChange]):
    output, report = DocxExporter().apply_changes_with_report(raw, changes)
    return DocxDocument(io.BytesIO(output)), output, report


def _styled_document() -> bytes:
    document = DocxDocument()
    document.add_paragraph("Нетронутый абзац.")
    paragraph = document.add_paragraph("Срок ответа ")
    paragraph.add_run("10 рабочих").bold = True
    paragraph.add_run(" дней, ")
    paragraph.add_run("курсив").italic = True
    paragraph.add_run(" в конце.")
    document.add_paragraph("Последний абзац.")
    return _save(document)


def test_replacement_inside_bold_run_keeps_neighbour_formatting() -> None:
    raw = _styled_document()
    document, _, report = _export(raw, [_change(raw, "10", "5")])
    assert _runs(document.paragraphs[1]) == [
        ("Срок ответа ", False, False),
        ("5 рабочих", True, False),
        (" дней, ", False, False),
        ("курсив", False, True),
        (" в конце.", False, False),
    ]
    assert (report.in_runs, report.rewritten, report.verified) == (1, 0, True)


def test_replacement_across_runs_takes_first_run_formatting() -> None:
    raw = _styled_document()
    document, _, _ = _export(raw, [_change(raw, "рабочих дней", "календарных дней")])
    assert _runs(document.paragraphs[1])[:2] == [
        ("Срок ответа ", False, False),
        ("10 календарных дней", True, False),
    ]
    assert document.paragraphs[1].text == "Срок ответа 10 календарных дней, курсив в конце."


def test_several_edits_in_one_paragraph() -> None:
    raw = _styled_document()
    changes = [_change(raw, "Срок", "Период"), _change(raw, "в конце", "в самом конце")]
    document, _, report = _export(raw, changes)
    assert document.paragraphs[1].text == "Период ответа 10 рабочих дней, курсив в самом конце."
    assert report.in_runs == 2
    assert _runs(document.paragraphs[1])[1] == ("10 рабочих", True, False)


def test_edits_at_paragraph_start_and_end() -> None:
    raw = _styled_document()
    changes = [_change(raw, "Срок ответа ", "Ответ: "), _change(raw, " в конце.", "!")]
    document, _, _ = _export(raw, changes)
    assert document.paragraphs[1].text == "Ответ: 10 рабочих дней, курсив!"


def test_deleting_whole_run_removes_it() -> None:
    raw = _styled_document()
    document, _, _ = _export(raw, [_change(raw, "курсив", "", kind="delete")])
    assert [text for text, _, _ in _runs(document.paragraphs[1])] == [
        "Срок ответа ",
        "10 рабочих",
        " дней, ",
        " в конце.",
    ]


def test_untouched_paragraphs_keep_identical_xml() -> None:
    raw = _styled_document()
    _, output, _ = _export(raw, [_change(raw, "10", "5")])

    def paragraphs_xml(data: bytes) -> list[bytes]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        root = parse_xml(xml)
        from lxml import etree

        return [etree.tostring(p) for p in root.iter(qn("w:p"))]

    before, after = paragraphs_xml(raw), paragraphs_xml(output)
    assert len(before) == len(after)
    assert [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] == [1]


def test_hyperlink_text_is_not_duplicated() -> None:
    document = DocxDocument()
    paragraph = document.add_paragraph("См. ")
    hyperlink = parse_xml(
        f'<w:hyperlink {nsdecls("w", "r")} r:id="rId99">'
        '<w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>старую ссылку</w:t></w:r>'
        "</w:hyperlink>"
    )
    paragraph._p.append(hyperlink)
    paragraph.add_run(" сегодня.")
    raw = _save(document)

    result, output, report = _export(raw, [_change(raw, "старую", "новую")])
    assert result.paragraphs[0].text == "См. новую ссылку сегодня."
    xml = zipfile.ZipFile(io.BytesIO(output)).read("word/document.xml")
    assert xml.count("новую".encode()) == 1
    assert b"rId99" in xml
    assert report.verified is True


def test_drawing_in_run_is_kept() -> None:
    document = DocxDocument()
    paragraph = document.add_paragraph("Схема ")
    run = paragraph.add_run("тут")
    run._r.append(parse_xml(f"<w:drawing {nsdecls('w')}/>"))
    paragraph.add_run(" и подпись.")
    raw = _save(document)

    result, _, _ = _export(raw, [_change(raw, "подпись", "описание")])
    assert result.paragraphs[0].runs[1]._r.find(qn("w:drawing")) is not None
    assert result.paragraphs[0].text == "Схема тут и описание."


def test_edit_in_table_cell() -> None:
    document = DocxDocument()
    document.add_paragraph("До таблицы.")
    table = document.add_table(rows=1, cols=2)
    cell = table.cell(0, 1).paragraphs[0]
    cell.add_run("Цена ")
    cell.add_run("100").bold = True
    cell.add_run(" руб.")
    raw = _save(document)

    result, _, report = _export(raw, [_change(raw, "100", "120")])
    runs = _runs(result.tables[0].cell(0, 1).paragraphs[0])
    assert runs == [("Цена ", False, False), ("120", True, False), (" руб.", False, False)]
    assert report.verified is True


def test_nbsp_outside_edit_survives() -> None:
    document = DocxDocument()
    document.add_paragraph("Лимит 50\u00a0МБ, срок 10 дней.")
    raw = _save(document)
    result, _, _ = _export(raw, [_change(raw, "10 дней", "5 дней")])
    assert result.paragraphs[0].text == "Лимит 50\u00a0МБ, срок 5 дней."


def test_edit_in_field_result_is_not_applied() -> None:
    document = DocxDocument()
    paragraph = document.add_paragraph("Страница ")
    for xml in (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        "<w:r><w:t>7</w:t></w:r>",
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ):
        paragraph._p.append(parse_xml(xml.replace("<w:r>", f"<w:r {nsdecls('w')}>", 1)))
    paragraph.add_run(" из 10.")
    raw = _save(document)

    result, _, report = _export(raw, [_change(raw, "7", "8"), _change(raw, "10", "12")])
    assert result.paragraphs[0].text == "Страница 7 из 12."
    assert (report.in_runs, report.unapplied) == (1, 1)
    assert "правка в результате поля" in report.reasons


def test_edit_over_tab_falls_back_to_rewrite() -> None:
    document = DocxDocument()
    paragraph = document.add_paragraph()
    paragraph.add_run("Имя\tЗначение").bold = True
    raw = _save(document)
    result, _, report = _export(raw, [_change(raw, "Имя\tЗнач", "Ключ\tЗнач")])
    assert result.paragraphs[0].text == "Ключ\tЗначение"
    assert (report.in_runs, report.rewritten, report.verified) == (0, 1, True)


def test_unanchored_change_goes_through_same_run_replacement() -> None:
    raw = _styled_document()
    change = AppliedChange(section_ref="", change_type="modify", old_text="10", new_text="5")
    document, _, report = _export(raw, [change])
    assert _runs(document.paragraphs[1])[1] == ("5 рабочих", True, False)
    assert report.in_runs == 1


def test_add_after_list_item_continues_list_and_font() -> None:
    document = DocxDocument()
    document.add_paragraph("Пункт один", style="List Bullet").runs[0].italic = True
    document.add_paragraph("Обычный текст.")
    raw = _save(document)
    change = AppliedChange(
        section_ref="", change_type="add", old_text="Пункт один", new_text="Пункт два"
    )
    result, _, report = _export(raw, [change])
    added = result.paragraphs[1]
    assert added.text == "Пункт два"
    assert added.style.name == "List Bullet"
    assert added.runs[0].italic is True
    assert report.verified is True


def test_add_after_heading_takes_next_paragraph_formatting() -> None:
    document = DocxDocument()
    document.add_heading("Сроки", level=1)
    body = document.add_paragraph("Текст раздела.")
    body.runs[0].bold = True
    raw = _save(document)
    change = AppliedChange(section_ref="", change_type="add", old_text="Сроки", new_text="Новое")
    result, _, _ = _export(raw, [change])
    added = result.paragraphs[1]
    assert added.text == "Новое"
    assert added.style.name == "Normal"
    assert added.runs[0].bold is True


def test_no_changes_returns_original_bytes() -> None:
    raw = _styled_document()
    assert DocxExporter().apply_changes(raw, []) == raw


def test_tracked_insertion_is_outside_text_and_kept() -> None:
    """Раны внутри w:ins не входят в paragraph.text: их не видит ни разбор, ни правка."""
    document = DocxDocument()
    paragraph = document.add_paragraph("Срок 10 дней")
    paragraph._p.append(
        parse_xml(
            f'<w:ins {nsdecls("w")} w:id="1" w:author="A" w:date="2026-01-01T00:00:00Z">'
            "<w:r><w:t> вставлено</w:t></w:r></w:ins>"
        )
    )
    raw = _save(document)
    assert DocxParser().parse(raw).plain_text == "Срок 10 дней"

    result, _, report = _export(raw, [_change(raw, "10", "5")])
    assert result.paragraphs[0].text == "Срок 5 дней"
    assert result.paragraphs[0]._p.find(qn("w:ins")) is not None
    assert report.verified is True
