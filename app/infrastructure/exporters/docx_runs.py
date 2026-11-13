"""Замена текста внутри ранов абзаца .docx с сохранением оформления.

Абзац раскладывается на атомы в том же порядке, в каком python-docx собирает
paragraph.text: раны абзаца и раны гиперссылок, внутри рана элементы w:t, w:tab, w:br,
w:cr, w:noBreakHyphen и w:ptab. Править можно только w:t: текст замены пишется в первый
затронутый атом и получает его оформление, покрытый текст следующих атомов удаляется,
остальные элементы рана остаются на месте.

Правки заданы в нормализованном тексте абзаца (normalize_text). Замена неразрывного
пробела обычным длину не меняет, и смещения совпадают; NFC и удаление ZWSP меняют, и для
такого атома строится карта по кластерам «основной символ и комбинируемые знаки».

Раны между w:fldChar начала и конца (оглавление, номера страниц, перекрёстные ссылки)
не правятся: Word пересчитает результат поля и молча вернёт старый текст. Раны внутри
исправлений Word (w:ins, w:del) не являются прямыми детьми абзаца, python-docx не
включает их в paragraph.text, поэтому они не участвуют ни в разборе, ни в правке.
"""

from __future__ import annotations

import copy
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from app.domain.text_normalization import normalize_text
from app.infrastructure.exporters.text_edits import TextEdit

_TEXT_TAGS = frozenset(
    qn(tag) for tag in ("w:t", "w:tab", "w:br", "w:cr", "w:noBreakHyphen", "w:ptab")
)
_W_T = qn("w:t")
_W_RPR = qn("w:rPr")
_FLD_CHAR = qn("w:fldChar")
_FLD_CHAR_TYPE = qn("w:fldCharType")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


class Unmappable(Exception):
    """Атомы абзаца нельзя сопоставить с его нормализованным текстом."""


class EditOutcome(StrEnum):
    IN_RUNS = "in_runs"
    REWRITE = "rewrite"
    FIELD = "field"


@dataclass(slots=True)
class _Atom:
    element: Any
    run: Any
    raw: str
    norm_start: int
    norm_end: int
    # Пары (смещение в нормализованном тексте атома, смещение в raw) на границах
    # кластеров; None — нормализация не меняет длину и смещения совпадают.
    bounds: list[tuple[int, int]] | None
    editable: bool
    in_field: bool
    changed: bool = False

    def to_raw(self, local: int, *, ceil: bool) -> int:
        if self.bounds is None:
            return local
        norms = [n for n, _ in self.bounds]
        index = bisect_left(norms, local) if ceil else bisect_right(norms, local) - 1
        index = max(0, min(index, len(self.bounds) - 1))
        return self.bounds[index][1]


def _cluster_bounds(raw: str, normalized: str) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    pieces: list[str] = []
    norm_offset = 0
    start = 0
    for index in range(1, len(raw) + 1):
        if index < len(raw) and unicodedata.combining(raw[index]):
            continue
        piece = normalize_text(raw[start:index])
        bounds.append((norm_offset, start))
        pieces.append(piece)
        norm_offset += len(piece)
        start = index
    bounds.append((norm_offset, len(raw)))
    if "".join(pieces) != normalized:
        raise Unmappable("нормализация атома зависит от соседних символов")
    return bounds


def paragraph_atoms(paragraph: Paragraph) -> list[_Atom]:
    """Атомы абзаца; O(R) по числу элементов его ранов."""
    atoms: list[_Atom] = []
    field_depth = 0
    norm_offset = 0
    raw_parts: list[str] = []
    for run in paragraph._p.xpath("w:r | w:hyperlink/w:r"):
        for child in run:
            if child.tag == _FLD_CHAR:
                kind = child.get(_FLD_CHAR_TYPE)
                if kind == "begin":
                    field_depth += 1
                elif kind == "end":
                    field_depth = max(0, field_depth - 1)
                continue
            if child.tag not in _TEXT_TAGS:
                continue
            raw = str(child) if child.tag != _W_T else (child.text or "")
            raw_parts.append(raw)
            normalized = normalize_text(raw)
            bounds = None if len(normalized) == len(raw) else _cluster_bounds(raw, normalized)
            atoms.append(
                _Atom(
                    element=child,
                    run=run,
                    raw=raw,
                    norm_start=norm_offset,
                    norm_end=norm_offset + len(normalized),
                    bounds=bounds,
                    editable=child.tag == _W_T,
                    in_field=field_depth > 0,
                )
            )
            norm_offset += len(normalized)
    if "".join(raw_parts) != paragraph.text:
        raise Unmappable("атомы не совпадают с текстом абзаца")
    if norm_offset != len(normalize_text(paragraph.text)):
        raise Unmappable("нормализация абзаца переходит границу рана")
    return atoms


def _touched(atoms: Sequence[_Atom], edit: TextEdit) -> list[_Atom]:
    return [a for a in atoms if a.norm_start < edit.end and a.norm_end > edit.start]


def classify(atoms: Sequence[_Atom], edit: TextEdit) -> EditOutcome:
    touched = _touched(atoms, edit)
    if any(atom.in_field for atom in touched):
        return EditOutcome.FIELD
    if not touched or not all(atom.editable for atom in touched):
        return EditOutcome.REWRITE
    return EditOutcome.IN_RUNS


def apply_in_runs(atoms: Sequence[_Atom], edits: Sequence[TextEdit]) -> None:
    """Правки без пересечений применяются справа налево: смещения левых правок не сдвигаются.

    Перед вызовом каждая правка проверена classify и затрагивает только w:t вне полей.
    """
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        touched = _touched(atoms, edit)
        first, last = touched[0], touched[-1]
        raw_start = first.to_raw(edit.start - first.norm_start, ceil=False)
        raw_end = last.to_raw(edit.end - last.norm_start, ceil=True)
        replacement = edit.replacement or ""
        if first is last:
            first.raw = first.raw[:raw_start] + replacement + first.raw[raw_end:]
        else:
            first.raw = first.raw[:raw_start] + replacement
            for atom in touched[1:-1]:
                atom.raw = ""
            last.raw = last.raw[raw_end:]
        for atom in touched:
            atom.changed = True
    _write_back(atoms)


def _write_back(atoms: Sequence[_Atom]) -> None:
    runs: dict[int, Any] = {}
    for atom in atoms:
        if not atom.changed:
            continue
        if atom.raw:
            atom.element.text = atom.raw
            atom.element.set(_XML_SPACE, "preserve")
        else:
            atom.element.getparent().remove(atom.element)
        runs[id(atom.run)] = atom.run
    # Ран, от которого осталось только оформление, удаляется: пустой ран Word хранит зря.
    for run in runs.values():
        if all(child.tag == _W_RPR for child in run):
            parent = run.getparent()
            if parent is not None:
                parent.remove(run)


def rewrite_paragraph(paragraph: Paragraph, new_text: str) -> None:
    """Запасной путь: абзац переписывается одним раном с оформлением первого рана."""
    runs = paragraph.runs
    for run in runs:
        run.text = ""
    if runs:
        runs[0].text = new_text
    else:
        paragraph.add_run(new_text)


def _is_heading(paragraph: Paragraph) -> bool:
    ppr = paragraph._p.pPr
    if ppr is not None and ppr.find(qn("w:outlineLvl")) is not None:
        return True
    style = paragraph.style
    name = (style.name if style is not None else "") or ""
    return name.lower().startswith(("heading", "заголовок", "title"))


def _template_for(anchor: Paragraph) -> Paragraph:
    """Оформление нового абзаца: якорь, а для заголовка — следующий обычный абзац."""
    if not _is_heading(anchor):
        return anchor
    sibling = anchor._p.getnext()
    while sibling is not None:
        if sibling.tag == qn("w:p"):
            candidate = Paragraph(sibling, anchor._parent)
            if not _is_heading(candidate):
                return candidate
        sibling = sibling.getnext()
    return anchor


def insert_paragraph_after(anchor: Paragraph, text: str) -> Paragraph:
    """Новый абзац сразу за якорем с его w:pPr и w:rPr последнего рана.

    Так пункт списка продолжает список, а шрифт совпадает с соседним текстом. Разрыв
    раздела (w:sectPr) из копии убирается, иначе вставка начала бы новый раздел.
    """
    template = _template_for(anchor)
    new_paragraph = anchor.insert_paragraph_before()
    anchor._p.addnext(new_paragraph._p)
    ppr = template._p.pPr
    if ppr is not None:
        ppr_copy = copy.deepcopy(ppr)
        for sect in ppr_copy.findall(qn("w:sectPr")):
            ppr_copy.remove(sect)
        existing = new_paragraph._p.pPr
        if existing is not None:
            new_paragraph._p.remove(existing)
        new_paragraph._p.insert(0, ppr_copy)
    run = new_paragraph.add_run(text)
    rpr = next(
        (r.find(_W_RPR) for r in reversed(template._p.xpath("w:r")) if r.find(_W_RPR) is not None),
        None,
    )
    if rpr is not None:
        existing_rpr = run._r.find(_W_RPR)
        if existing_rpr is not None:
            run._r.remove(existing_rpr)
        run._r.insert(0, copy.deepcopy(rpr))
    return new_paragraph
