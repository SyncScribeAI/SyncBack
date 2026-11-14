"""
Применяет принятые правки к txt/markdown так, чтобы всё вне затронутых строк осталось
байт в байт исходным: BOM, окончания строк (\n и \r\n), неразрывные пробелы, разложенные
формы Unicode и даже некорректные байты UTF-8.

Добавление (add) вставляется только по якорю: новый абзац встаёт сразу после строки
с первым вхождением old_text. Якоря нет или он не найден — правка не применяется,
как modify/delete с ненайденным old_text; место вставки за модель не выбираем.
Тот же порядок повторяет фронтенд в «Чистовике», чтобы экран и экспорт не расходились.

Координаты правок заданы в plain_text парсера, то есть в normalize_text(файл). Файл
делится по байту 0x0A, который не встречается внутри многобайтной последовательности
UTF-8, и каждая строка нормализуется отдельно: нормализация не переходит через перевод
строки, поэтому сумма нормализованных строк и есть plain_text. Правки с границами,
под которыми лежит их old_text, берутся как есть; остальные (прежний конвейер или
рассинхрон с файлом) ищутся по первому вхождению. Строка без правок пишется исходными
байтами, затронутая — нормализованным текстом с правкой и прежним окончанием строки.
"""

from bisect import bisect_right
from dataclasses import dataclass

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


_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class _RawLine:
    body: bytes
    eol: bytes
    text: str


def _split_lines(raw_bytes: bytes) -> list[_RawLine]:
    lines: list[_RawLine] = []
    parts = raw_bytes.split(b"\n")
    last = len(parts) - 1
    for index, part in enumerate(parts):
        eol = b"" if index == last else b"\n"
        # normalize_text сворачивает только пару \r\n, одиночный \r остаётся в тексте.
        if eol and part.endswith(b"\r"):
            part, eol = part[:-1], b"\r\n"
        lines.append(_RawLine(part, eol, normalize_text(part.decode("utf-8", errors="replace"))))
    return lines


def _resolve(text: str, changes: list[AppliedChange]) -> list[TextEdit]:
    """Правки в координатах plain_text: по границам или по первому вхождению."""
    edits: list[TextEdit] = []
    for change in changes:
        edit = anchored_edit(text, change) or _searched_edit(text, change)
        if edit is not None:
            edits.append(edit)
    return edits


def _searched_edit(text: str, change: AppliedChange) -> TextEdit | None:
    if change.change_type == "add":
        anchor = normalize_text(change.old_text or "").strip()
        position = _anchor_line_end(text, anchor) if anchor and change.new_text else None
        if position is None:
            return None
        return TextEdit(position, position, block=(change.new_text or "").strip("\n"))
    old_text = normalize_text(change.old_text or "")
    if change.change_type not in ("modify", "delete") or not old_text:
        return None
    start = text.find(old_text)
    if start < 0:
        return None
    replacement = change.new_text or "" if change.change_type == "modify" else ""
    return TextEdit(start, start + len(old_text), replacement=replacement)


def _non_overlapping(edits: list[TextEdit]) -> list[TextEdit]:
    """Те же правила, что у apply_edits: правка внутри уже применённой пропускается,
    несколько вставок в одну точку идут в порядке выдачи."""
    kept: list[TextEdit] = []
    cursor = 0
    last_insert = -1
    for edit in sorted(edits, key=lambda e: e.start):
        same_point = edit.replacement is None and edit.start == last_insert
        if edit.start < cursor and not same_point:
            continue
        kept.append(edit)
        if edit.replacement is None:
            last_insert = cursor = edit.start
        else:
            cursor = edit.end
    return kept


class TextExporter:
    def apply_changes(self, raw_bytes: bytes, changes: list[AppliedChange]) -> bytes:
        """O(n + k log k): n — длина файла, k — число правок."""
        lines = _split_lines(raw_bytes)
        starts: list[int] = []
        offset = 0
        for line in lines:
            starts.append(offset)
            offset += len(line.text) + 1
        text = "\n".join(line.text for line in lines)
        edits = _non_overlapping(_resolve(text, changes))
        if not edits:
            return raw_bytes
        default_eol = next((line.eol for line in lines if line.eol), b"\n")

        out: list[bytes] = []
        line_index = 0
        position = 0
        while position < len(edits):
            first = bisect_right(starts, edits[position].start) - 1
            last = _last_line(starts, edits[position])
            group_end = position + 1
            while (
                group_end < len(edits) and bisect_right(starts, edits[group_end].start) - 1 <= last
            ):
                last = max(last, _last_line(starts, edits[group_end]))
                group_end += 1
            out.extend(line.body + line.eol for line in lines[line_index:first])
            out.append(
                _render_region(lines, starts, first, last, edits[position:group_end], default_eol)
            )
            line_index = last + 1
            position = group_end
        out.extend(line.body + line.eol for line in lines[line_index:])
        return b"".join(out)


def _last_line(starts: list[int], edit: TextEdit) -> int:
    """Последняя строка, которую меняет правка.

    Правка, захватившая перевод строки, склеивает строку со следующей, поэтому граница
    сразу после перевода строки включает следующую строку в область.
    """
    end = edit.end if edit.replacement is not None else edit.start
    return bisect_right(starts, end) - 1


def _render_region(
    lines: list[_RawLine],
    starts: list[int],
    first: int,
    last: int,
    edits: list[TextEdit],
    default_eol: bytes,
) -> bytes:
    """Затронутые строки first..last: нормализованный текст с правками и прежние окончания."""
    region = lines[first : last + 1]
    base = starts[first]
    region_text = "\n".join(line.text for line in region)
    shifted = [
        TextEdit(e.start - base, e.end - base, replacement=e.replacement, block=e.block)
        for e in edits
    ]
    result = apply_edits(region_text, shifted)
    eol = next((line.eol for line in region if line.eol), default_eol)
    encoded = result.encode("utf-8")
    if eol != b"\n":
        encoded = encoded.replace(b"\n", eol)
    if first == 0 and lines[0].body.startswith(_BOM):
        encoded = _BOM + encoded
    if result.endswith("\n") and edits[-1].replacement is None:
        # Абзац вставлен в конце области: apply_edits уже закрыл его переводом строки, а
        # пустая строка перед следующей непустой строкой файла нужна та же, что и раньше.
        following = lines[last + 1] if last + 1 < len(lines) else None
        if following is not None and following.text.strip():
            encoded += eol
        return encoded
    return encoded + region[-1].eol
