"""PDF как документ и как источник: текст, блоки и положение текста на страницах.

PDFium вызывается только под общим замком и только на время чтения страниц; сборка
блоков идёт уже без замка, чтобы параллельный разбор другого файла не ждал её.
"""

import ctypes
from collections.abc import Sequence

import pypdfium2.raw as pdfium_c

from app.domain.document_geometry import PageRect, TextRuns
from app.domain.exceptions import PdfHasNoTextLayerError
from app.domain.interfaces.document_parser import ParsedDocument
from app.domain.source_text import SourceLayout, serialize_source_layout
from app.infrastructure.parsers.pdf_blocks import PdfLayout, build_layout
from app.infrastructure.parsers.pdf_lines import iter_pages, open_pdf

# Совпадает со значением настройки pdf_max_pages по умолчанию.
DEFAULT_PDF_MAX_PAGES = 500

NO_TEXT_LAYER_MESSAGE = (
    "В PDF нет текстового слоя: это скан или изображение. Загрузите PDF с текстовым "
    "слоем либо исходный DOCX"
)


class PdfParser:
    def __init__(self, max_pages: int = DEFAULT_PDF_MAX_PAGES) -> None:
        self._max_pages = max_pages

    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        return self.parse_with_runs(raw_bytes)[0]

    def parse_with_runs(self, raw_bytes: bytes) -> tuple[ParsedDocument, TextRuns]:
        return self.layout(raw_bytes).to_document()

    def extract_text(self, raw_bytes: bytes) -> str:
        return self.parse(raw_bytes).plain_text

    def extract_source_text(self, raw_bytes: bytes) -> str:
        return self.extract_source_layout(raw_bytes).text

    def extract_source_layout(self, raw_bytes: bytes) -> SourceLayout:
        return serialize_source_layout(self.layout(raw_bytes).iter_source_fragments())

    def layout(self, raw_bytes: bytes) -> PdfLayout:
        with open_pdf(raw_bytes, self._max_pages) as document:
            pages = list(iter_pages(document))
        if all(not page.lines for page in pages):
            raise PdfHasNoTextLayerError(NO_TEXT_LAYER_MESSAGE)
        return build_layout(pages)


class PdfiumGeometry:
    """Реализация DocumentGeometry для PDF."""

    def __init__(self, parser: PdfParser) -> None:
        self._parser = parser
        self._max_pages = parser._max_pages

    def parse_with_runs(self, raw_bytes: bytes) -> tuple[ParsedDocument, TextRuns]:
        return self._parser.parse_with_runs(raw_bytes)

    def locate(
        self, raw_bytes: bytes, runs: TextRuns, spans: Sequence[tuple[int, int]]
    ) -> list[tuple[PageRect, ...]]:
        """Сначала все куски группируются по страницам, затем каждая затронутая страница
        открывается один раз. Прямоугольники строк считает PDFium (FPDFText_GetRect):
        он объединяет соседние символы строки, поэтому их число порядка числа строк."""
        by_page: dict[int, list[tuple[int, int, int]]] = {}
        for span_index, (start, end) in enumerate(spans):
            for page, char_index, count in runs.pieces(start, end):
                by_page.setdefault(page, []).append((span_index, char_index, count))
        result: list[list[PageRect]] = [[] for _ in spans]
        if not by_page:
            return [()] * len(spans)
        left, top, right, bottom = (ctypes.c_double() for _ in range(4))
        with open_pdf(raw_bytes, self._max_pages) as document:
            for page_number in sorted(by_page):
                page = document[page_number - 1]
                try:
                    textpage = page.get_textpage()
                    try:
                        for span_index, char_index, count in by_page[page_number]:
                            rect_count = pdfium_c.FPDFText_CountRects(textpage, char_index, count)
                            for k in range(rect_count):
                                pdfium_c.FPDFText_GetRect(
                                    textpage,
                                    k,
                                    ctypes.byref(left),
                                    ctypes.byref(top),
                                    ctypes.byref(right),
                                    ctypes.byref(bottom),
                                )
                                result[span_index].append(
                                    PageRect(
                                        page_number,
                                        left.value,
                                        bottom.value,
                                        right.value,
                                        top.value,
                                    )
                                )
                    finally:
                        textpage.close()
                finally:
                    page.close()
        return [tuple(rects) for rects in result]
