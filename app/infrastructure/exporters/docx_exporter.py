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
"""

import io
from typing import Any

from docx import Document as DocxDocument
from docx.text.paragraph import Paragraph

from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.text_normalization import normalize_text
from app.infrastructure.docx_paragraphs import iter_docx_paragraphs


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


class DocxExporter:
    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes:
        document = DocxDocument(io.BytesIO(raw_bytes))
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
