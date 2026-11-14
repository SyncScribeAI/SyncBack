"""Набор P0 для проверки PDF-парсера (docs/pdf-spike.md).

DOCX собираются python-docx и переводятся в PDF через LibreOffice; скан с текстовым
слоем делает tesseract, повёрнутую страницу и шифрование qpdf. Нужны soffice,
pdftoppm, tesseract и qpdf. Запуск: python scripts/pdf_spike/make_corpus.py <каталог>
"""

import subprocess
import sys
from pathlib import Path

import pypdfium2 as pdfium
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

LOREM = (
    "Сервис хранит загруженные файлы не дольше тридцати дней. Максимальный размер "
    "файла составляет 50 МБ, превышение отклоняется с понятным сообщением. "
)


def _borders(table, on: bool) -> None:
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single" if on else "nil")
        element.set(qn("w:sz"), "4")
        borders.append(element)
    table._tbl.tblPr.append(borders)


def _table(document, rows: int, cols: int, bordered: bool):
    table = document.add_table(rows=rows, cols=cols)
    _borders(table, bordered)
    header = ["Параметр", "Значение", "Комментарий", "Ед."]
    values = ["timeout", "50", "Время ожидания ответа", "с"]
    for c in range(cols):
        table.cell(0, c).text = header[c]
    for r in range(1, rows):
        for c in range(cols):
            table.cell(r, c).text = values[c] + (f" {r}" if c == 0 else "")
    return table


def _page_field(paragraph) -> None:
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), kind)
        else:
            element = OxmlElement("w:instrText")
            element.text = text
        run._r.append(element)


def make_docx(folder: Path) -> None:
    def save(document, name: str) -> None:
        document.save(folder / f"{name}.docx")

    d = Document()
    for _ in range(6):
        d.add_paragraph(LOREM * 4)
    save(d, "01_plain")

    d = Document()
    d.add_heading("Руководство администратора", 1)
    d.add_paragraph(LOREM * 2)
    d.add_heading("Установка", 2)
    d.add_paragraph(LOREM)
    for text in ["Скачайте архив", "Распакуйте его", "Запустите установщик"]:
        d.add_paragraph(text, style="List Bullet")
    d.add_heading("Настройка", 2)
    for text in ["Откройте конфигурацию", "Задайте адрес сервера", "Перезапустите службу"]:
        d.add_paragraph(text, style="List Number")
    d.add_paragraph(LOREM)
    save(d, "02_headings_lists")

    for name, bordered in (("03_table_borders", True), ("04_table_noborders", False)):
        d = Document()
        d.add_paragraph("Таблица параметров " + ("с рамками." if bordered else "без рамок."))
        _table(d, 6, 3, bordered)
        d.add_paragraph(LOREM)
        save(d, name)

    d = Document()
    d.add_paragraph("Объединённые ячейки.")
    t = _table(d, 5, 4, True)
    t.cell(1, 1).merge(t.cell(1, 2)).text = "Объединено по горизонтали"
    t.cell(2, 3).merge(t.cell(4, 3)).text = "Вертикально"
    save(d, "05_table_merged")

    d = Document()
    d.add_paragraph(LOREM * 6)
    _table(d, 60, 3, True)
    d.add_paragraph("После таблицы.")
    save(d, "06_table_pagebreak")

    d = Document()
    section = d.sections[0]
    section.header.paragraphs[0].text = "ООО Ромашка. Регламент эксплуатации"
    section.footer.paragraphs[0].text = "Конфиденциально"
    _page_field(section.footer.add_paragraph())
    for _ in range(14):
        d.add_paragraph(LOREM * 3)
    save(d, "07_headers_footers")

    d = Document()
    section = d.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    d.add_heading("Альбомная страница", 1)
    d.add_paragraph(LOREM * 3)
    _table(d, 4, 4, True)
    save(d, "08_landscape")

    d = Document()
    d.add_heading("Мелкий и крупный кегль", 1)
    p = d.add_paragraph()
    p.add_run("Сноска мелким шрифтом. ").font.size = Pt(8)
    p.add_run(LOREM).font.size = Pt(14)
    d.add_paragraph(
        "Строка с дефисом при переносе: высокопроизводительный, многопользовательский, "
        "кроссплатформенный " * 5
    )
    save(d, "09_fonts_hyphen")

    d = Document()
    d.add_paragraph(
        "Mixed English and русский text. Version 2.4.1, path /etc/syncback.conf, "
        "URL https://example.com/a?b=c."
    )
    d.add_paragraph("Цитата «в кавычках» — с тире и неразрывным\u00a0пробелом.")
    save(d, "10_mixed")


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def make_pdfs(folder: Path) -> None:
    docx = sorted(p.name for p in folder.glob("*.docx"))
    _run("soffice", "--headless", "--convert-to", "pdf", "--outdir", ".", *docx, cwd=folder)

    # Скан: страница в картинку и распознавание; в tesseract нет русского, поэтому
    # берётся страница с английским текстом.
    _run("pdftoppm", "-r", "200", "-png", "-f", "1", "-l", "1", "10_mixed.pdf", "scan", cwd=folder)
    _run("tesseract", "scan-1.png", "11_scan_ocr", "-l", "eng", "pdf", cwd=folder)

    _run(
        "pdftoppm",
        "-r",
        "100",
        "-jpeg",
        "-f",
        "1",
        "-l",
        "1",
        "02_headings_lists.pdf",
        "img",
        cwd=folder,
    )
    pdf = pdfium.PdfDocument.new()
    image = pdfium.PdfImage.new(pdf)
    image.load_jpeg(str(folder / "img-1.jpg"))
    image.set_matrix(pdfium.PdfMatrix().scale(595, 842))
    page = pdf.new_page(595, 842)
    page.insert_obj(image)
    page.gen_content()
    pdf.save(str(folder / "12_image_only.pdf"))

    _run(
        "qpdf",
        "--encrypt",
        "user",
        "owner",
        "256",
        "--",
        "01_plain.pdf",
        "13_encrypted.pdf",
        cwd=folder,
    )
    _run("qpdf", "03_table_borders.pdf", "--rotate=+90:1", "14_rotated.pdf", cwd=folder)

    reader = PdfReader(folder / "02_headings_lists.pdf")
    writer = PdfWriter()
    for page in reader.pages:
        page.cropbox = RectangleObject([30, 40, 560, 760])
        writer.add_page(page)
    writer.write(folder / "15_cropbox_offset.pdf")

    for leftover in [*folder.glob("scan-*.png"), *folder.glob("img-*.jpg")]:
        leftover.unlink()


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    make_docx(target)
    make_pdfs(target)
