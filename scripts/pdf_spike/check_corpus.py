"""Проверки набора P0 (docs/pdf-spike.md): текст против исходного DOCX, таблицы,
соответствие индексов символов PDFium и прямоугольников, отметка StrikeOut.

Запуск: python scripts/pdf_spike/check_corpus.py <каталог набора>
"""

import difflib
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from docx import Document
from docx.text.paragraph import Paragraph
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

from app.domain.exceptions import UnreadablePdfError
from app.infrastructure.docx_paragraphs import iter_docx_body_items, table_rows
from app.infrastructure.parsers.pdf_parser import PdfiumGeometry, PdfParser

_WORD = re.compile(r"\w+")


def docx_words(path: Path) -> list[str]:
    """Слова DOCX в порядке тела документа; объединённая ячейка считается один раз."""
    words: list[str] = []
    for item in iter_docx_body_items(Document(str(path))):
        if isinstance(item, Paragraph):
            words += _WORD.findall(item.text)
        else:
            for cells in table_rows(item):
                for cell in cells:
                    words += _WORD.findall(cell)
    return words


def check_runs(raw: bytes, plain: str, runs) -> tuple[int, int]:
    """Текст каждого отрезка карты совпадает с символами PDFium по тем же индексам."""
    bad = 0
    pdf = pdfium.PdfDocument(raw)
    pages: dict[int, pdfium.PdfTextPage] = {}
    for i in range(len(runs)):
        page = runs.pages[i]
        tp = pages.get(page) or pages.setdefault(page, pdf[page - 1].get_textpage())
        start, ci, count = runs.starts[i], runs.char_indexes[i], runs.counts[i]
        ours = plain[start : start + count].replace(" ", "")
        theirs = "".join(
            chr(pdfium_c.FPDFText_GetUnicode(tp, ci + k))
            for k in range(count)
            if not chr(pdfium_c.FPDFText_GetUnicode(tp, ci + k)).isspace()
        )
        theirs = theirs.replace("\uf0b7", "•")
        if ours != theirs:
            bad += 1
    return bad, len(runs)


def strike_first_word(raw: bytes, geometry: PdfiumGeometry, word: str, out: Path):
    layout = geometry.parse_layout(raw)
    document, runs = layout.document, layout.runs
    assert runs is not None
    start = document.plain_text.find(word)
    if start < 0:
        return False
    rects = geometry.locate(raw, runs, [(start, start + len(word))])[0]
    writer = PdfWriter(io.BytesIO(raw), incremental=True)
    page = writer.pages[rects[0].page - 1]
    quads = []
    for r in rects:
        quads += [r.x0, r.y1, r.x1, r.y1, r.x0, r.y0, r.x1, r.y0]
    annot = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/StrikeOut"),
            NameObject("/Rect"): ArrayObject(
                [FloatObject(v) for v in (rects[0].x0, rects[0].y0, rects[-1].x1, rects[-1].y1)]
            ),
            NameObject("/QuadPoints"): ArrayObject([FloatObject(v) for v in quads]),
            NameObject("/C"): ArrayObject([FloatObject(1), FloatObject(0), FloatObject(0)]),
            NameObject("/Contents"): TextStringObject("замена"),
            NameObject("/T"): TextStringObject("SyncScribe"),
        }
    )
    writer.add_annotation(page, annot)
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    out.write_bytes(data)
    return data.startswith(raw), rects[0]


_DPI = 72


def red_box(pdf_path: Path, page: int) -> tuple[int, tuple[int, int, int, int] | None]:
    """Число красных пикселей и их рамка на рендере poppler в 72 dpi (1 пиксель = 1 пт)."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "pdftoppm",
                "-cropbox",
                "-r",
                str(_DPI),
                "-f",
                str(page),
                "-l",
                str(page),
                str(pdf_path),
                f"{tmp}/p",
            ],
            check=True,
        )
        data = next(Path(tmp).iterdir()).read_bytes()
    _, size, _, body = data.split(b"\n", 3)
    width = int(size.split()[0])
    xs, ys = [], []
    for i in range(0, len(body) - 2, 3):
        if body[i] > 200 and body[i + 1] < 80 and body[i + 2] < 80:
            xs.append((i // 3) % width)
            ys.append((i // 3) // width)
    if not xs:
        return 0, None
    return len(xs), (min(xs), min(ys), max(xs), max(ys))


def expected_box(raw: bytes, rect) -> tuple[float, float, float, float]:
    """Прямоугольник PDFium в пикселях области просмотра: учёт CropBox и /Rotate."""
    page = pdfium.PdfDocument(raw)[rect.page - 1]
    cx0, cy0, cx1, cy1 = page.get_cropbox()
    if page.get_rotation() == 90:
        return (rect.y0 - cy0, rect.x0 - cx0, rect.y1 - cy0, rect.x1 - cx0)
    return (rect.x0 - cx0, cy1 - rect.y1, rect.x1 - cx0, cy1 - rect.y0)


def main(folder: Path) -> None:
    parser = PdfParser()
    geometry = PdfiumGeometry(parser)
    for pdf_path in sorted(folder.glob("*.pdf")):
        raw = pdf_path.read_bytes()
        try:
            document, runs = parser.parse_with_runs(raw)
        except UnreadablePdfError as exc:
            print(f"{pdf_path.name}: отказ {type(exc).__name__}")
            continue
        words = _WORD.findall(document.plain_text)
        docx = pdf_path.with_suffix(".docx")
        ratio = (
            difflib.SequenceMatcher(None, docx_words(docx), words, autojunk=False).ratio()
            if docx.exists()
            else None
        )
        bad, total = check_runs(raw, document.plain_text, runs)
        tables = sum(1 for b in document.blocks if b.type.value == "table")
        strike_ok = None
        if pdf_path.stem.startswith(("02", "14", "15")):
            out = Path(tempfile.gettempdir()) / f"strike_{pdf_path.name}"
            word = "Комментарий" if pdf_path.stem.startswith("14") else "Установка"
            prefix, rect = strike_first_word(raw, geometry, word, out)
            count, box = red_box(out, 1)
            want = expected_box(raw, rect)
            # Отметка рисуется поверх строки, поэтому её рамка лежит внутри рамки слова.
            inside = box is not None and (
                want[0] - 3 <= box[0]
                and box[2] <= want[2] + 3
                and want[1] - 3 <= box[1]
                and box[3] <= want[3] + 3
            )
            strike_ok = f"начало совпадает {prefix}, красных пикселей {count}, внутри рамки {inside} {box} {tuple(round(v) for v in want)}"
        print(
            f"{pdf_path.name}: блоков {len(document.blocks)}, таблиц {tables}, "
            f"совпадение слов с DOCX {ratio if ratio is None else round(ratio, 3)}, "
            f"отрезков {total}, расхождений карты {bad}, зачёркивание {strike_ok}"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
