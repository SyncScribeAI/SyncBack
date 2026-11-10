"""
Применяет принятые правки к txt/markdown простой текстовой заменой. Посимвольный diff
сознательно не делаем на этой итерации.

Добавление (add) вставляется только по якорю: новый абзац встаёт сразу после строки
с первым вхождением old_text. Якоря нет или он не найден — правка не применяется,
как modify/delete с ненайденным old_text; место вставки за модель не выбираем.
Тот же порядок повторяет фронтенд в «Чистовике», чтобы экран и экспорт не расходились.

Текст файла и фрагменты правок перед сравнением нормализуются (см. normalize_text):
модель возвращает обычные пробелы там, где в файле неразрывные. Побочный эффект —
в экспортированном файле NBSP заменяются на обычные пробелы.

Нормализованный текст файла и есть plain_text парсера txt/markdown, поэтому границы
правки (start_offset/end_offset) указывают прямо в него. Правки с границами, под которыми
лежит их old_text, применяются одним проходом (text_edits) — так каждое из повторов фразы
меняется в своём месте. Остальные (прежний конвейер или рассинхрон с файлом) после этого
применяются поиском первого вхождения.
"""

from app.domain.interfaces.document_exporter import AppliedChange
from app.domain.text_normalization import normalize_text
from app.infrastructure.exporters.text_edits import TextEdit, apply_edits


def _insert_block(text: str, position: int, block: str) -> str:
    """Вставить абзац в позицию, оставив ровно одну пустую строку вокруг него."""
    head = text[:position].rstrip("\n")
    tail = text[position:].lstrip("\n")
    parts = [head] if head else []
    parts.append(block)
    joined = "\n\n".join(parts)
    return joined + ("\n\n" + tail if tail else "\n")


def _anchor_line_end(text: str, anchor: str) -> int | None:
    """Конец строки, в которой заканчивается первое вхождение якоря; None, если якоря нет."""
    index = text.find(anchor)
    if index < 0:
        return None
    newline = text.find("\n", index + len(anchor))
    return len(text) if newline < 0 else newline


def place_addition(text: str, change: AppliedChange) -> str:
    """Вставить добавление после строки с якорем; без найденного якоря вернуть текст как есть."""
    anchor = normalize_text(change.old_text or "").strip()
    if not anchor:
        return text
    position = _anchor_line_end(text, anchor)
    if position is None:
        return text
    return _insert_block(text, position, (change.new_text or "").strip("\n"))


def anchored_edit(text: str, change: AppliedChange) -> TextEdit | None:
    """Правка по её границам или None, если границ нет или под ними другой текст."""
    if not change.has_span:
        return None
    old_text = normalize_text(change.old_text or "")
    start, end = change.start_offset or 0, change.end_offset or 0
    if not old_text.strip() or text[start:end] != old_text:
        return None
    if change.change_type == "modify":
        return TextEdit(start, end, replacement=change.new_text or "")
    if change.change_type == "delete":
        return TextEdit(start, end, replacement="")
    if change.change_type == "add" and change.new_text:
        newline = text.find("\n", end)
        position = len(text) if newline < 0 else newline
        return TextEdit(position, position, block=change.new_text.strip("\n"))
    return None


class TextExporter:
    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes:
        text = normalize_text(raw_bytes.decode("utf-8", errors="replace"))
        edits: list[TextEdit] = []
        unanchored: list[AppliedChange] = []
        for change in changes:
            edit = anchored_edit(text, change)
            if edit is None:
                unanchored.append(change)
            else:
                edits.append(edit)
        if edits:
            text = apply_edits(text, edits)
        for change in unanchored:
            old_text = normalize_text(change.old_text or "")
            if change.change_type == "modify" and old_text:
                text = text.replace(old_text, change.new_text or "", 1)
            elif change.change_type == "delete" and old_text:
                text = text.replace(old_text, "", 1)
            elif change.change_type == "add" and change.new_text:
                text = place_addition(text, change)
        return text.encode("utf-8")
