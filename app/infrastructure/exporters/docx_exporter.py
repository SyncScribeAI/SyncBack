"""
Применяет принятые правки к docx: абзац с вхождением old_text переписывается целиком
(все runs очищаются и заменяются одним run с новым текстом — внутреннее форматирование
изменённого абзаца не сохраняется, это упрощение MVP).

Добавление (add) вставляется только по якорю — новым абзацем сразу после абзаца с первым
вхождением old_text, как в TextExporter. Якоря нет или он не найден — правка не применяется;
место вставки за модель не выбираем.

Текст абзаца и фрагменты правок сравниваются после normalize_text: в docx часто стоят
неразрывные пробелы, а модель возвращает обычные. Нетронутые абзацы остаются как есть;
в переписанном абзаце NBSP заменяются на обычные пробелы.

Абзацы перебираются в том же порядке, что и в DocxParser (тело и ячейки таблиц), —
иначе «первое вхождение» old_text в plain_text и в файле указывало бы на разные места.

По тому же обходу восстанавливаются начала абзацев в plain_text (абзацы склеены через
«\n»), поэтому правка с границами (start_offset/end_offset) попадает в свой абзац
бинарным поиском, без перебора документа. Такие правки применяются, только если текст
абзаца под границами совпадает с old_text; все правки абзаца собираются за один проход
(text_edits), и абзац переписывается один раз. Остальные правки после этого ищут первое
вхождение, как раньше. Итого O(P + S log S + T) вместо O(S·P) для правок с границами.
"""

import io
from bisect import bisect_right
from typing import Any

from docx import Document as DocxDocument
from docx.text.paragraph import Paragraph

from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.text_normalization import normalize_text
from app.infrastructure.docx_paragraphs import iter_docx_paragraphs
from app.infrastructure.exporters.text_edits import TextEdit, apply_edits


def _set_paragraph_text(paragraph: Paragraph, new_text: str) -> None:
    for run in paragraph.runs:
        run.text = ""
    if paragraph.runs:
        paragraph.runs[0].text = new_text
    else:
        paragraph.add_run(new_text)


def _insert_paragraph_after(paragraph: Paragraph, text: str) -> Paragraph:
    # python-docx умеет только insert_paragraph_before; переставляем созданный элемент за якорь.
    new_paragraph = paragraph.insert_paragraph_before(text)
    paragraph._p.addnext(new_paragraph._p)
    return new_paragraph


def _place_addition(document: Any, change: AppliedChange) -> None:
    anchor = normalize_text(change.old_text or "").strip()
    if not anchor:
        return
    for item in iter_docx_paragraphs(document):
        paragraph = item.paragraph
        if anchor in normalize_text(paragraph.text):
            _insert_paragraph_after(paragraph, change.new_text or "")
            return


class _ParagraphIndex:
    """Абзацы документа, их нормализованный текст и начала в plain_text парсера."""

    __slots__ = ("paragraphs", "starts", "texts")

    def __init__(self, document: Any) -> None:
        self.paragraphs: list[Paragraph] = []
        self.texts: list[str] = []
        self.starts: list[int] = []
        offset = 0
        for item in iter_docx_paragraphs(document):
            text = normalize_text(item.paragraph.text)
            self.paragraphs.append(item.paragraph)
            self.texts.append(text)
            self.starts.append(offset)
            offset += len(text) + 1

    def locate(self, change: AppliedChange) -> tuple[int, int, int] | None:
        """(номер абзаца, начало, конец) old_text внутри абзаца или None."""
        if not change.has_span or not self.starts:
            return None
        start, end = change.start_offset or 0, change.end_offset or 0
        index = bisect_right(self.starts, start) - 1
        if index < 0:
            return None
        local_start = start - self.starts[index]
        local_end = end - self.starts[index]
        text = self.texts[index]
        old_text = normalize_text(change.old_text or "")
        if not old_text.strip() or local_end > len(text):
            return None
        if text[local_start:local_end] != old_text:
            return None
        return index, local_start, local_end


def _apply_anchored(index: _ParagraphIndex, changes: list[AppliedChange]) -> list[AppliedChange]:
    """Применить правки с подтверждёнными границами; вернуть остальные."""
    edits: dict[int, list[TextEdit]] = {}
    additions: dict[int, list[str]] = {}
    rest: list[AppliedChange] = []
    for change in changes:
        located = index.locate(change)
        if located is None:
            rest.append(change)
            continue
        number, start, end = located
        if change.change_type == "modify":
            edits.setdefault(number, []).append(TextEdit(start, end, change.new_text or ""))
        elif change.change_type == "delete":
            edits.setdefault(number, []).append(TextEdit(start, end, ""))
        elif change.change_type == "add" and change.new_text:
            additions.setdefault(number, []).append(change.new_text)
        else:
            rest.append(change)
    for number, paragraph_edits in edits.items():
        _set_paragraph_text(
            index.paragraphs[number], apply_edits(index.texts[number], paragraph_edits)
        )
    for number, texts in additions.items():
        # addnext ставит абзац сразу за якорем, поэтому вставка с конца сохраняет порядок.
        for text in reversed(texts):
            _insert_paragraph_after(index.paragraphs[number], text)
    return rest


class DocxExporter:
    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes:
        document = DocxDocument(io.BytesIO(raw_bytes))
        # Индекс абзацев копирует их текст; без правок с границами он не нужен.
        if any(change.has_span for change in changes):
            changes = _apply_anchored(_ParagraphIndex(document), changes)
        for change in changes:
            old_text = normalize_text(change.old_text or "")
            if change.change_type in ("modify", "delete") and old_text:
                for item in iter_docx_paragraphs(document):
                    paragraph = item.paragraph
                    paragraph_text = normalize_text(paragraph.text)
                    if old_text in paragraph_text:
                        new_text = paragraph_text.replace(old_text, change.new_text or "", 1)
                        _set_paragraph_text(paragraph, new_text)
                        break
            elif change.change_type == "add" and change.new_text:
                _place_addition(document, change)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
