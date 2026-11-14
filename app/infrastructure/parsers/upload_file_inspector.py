import asyncio
from pathlib import PurePath
from typing import IO

from app.domain.exceptions import PdfHasNoTextLayerError
from app.domain.interfaces.file_storage import UploadContent
from app.domain.interfaces.upload_file_inspector import UploadFileReport
from app.infrastructure.parsers.pdf_lines import count_page_glyphs, open_pdf
from app.infrastructure.parsers.pdf_parser import NO_TEXT_LAYER_MESSAGE


class ParserUploadFileInspector:
    """Проверка PDF при загрузке документа или источника; остальные форматы проверять нечем.

    PDFium читает файл прямо из потока загрузки, без копии в памяти. Работа CPU-ёмкая и
    идёт под замком PDFium, поэтому выносится в поток, чтобы не держать цикл событий.
    """

    def __init__(self, pdf_max_pages: int) -> None:
        self._pdf_max_pages = pdf_max_pages

    async def inspect(self, filename: str, content: UploadContent) -> UploadFileReport:
        if PurePath(filename).suffix.lower() != ".pdf":
            return UploadFileReport()
        try:
            return await asyncio.to_thread(self._inspect_pdf, content.stream)
        finally:
            content.stream.seek(0)

    def _inspect_pdf(self, stream: IO[bytes]) -> UploadFileReport:
        stream.seek(0)
        with open_pdf(stream, self._pdf_max_pages) as document:
            page_count = len(document)
            empty = tuple(
                index + 1 for index in range(page_count) if not count_page_glyphs(document, index)
            )
        if len(empty) == page_count:
            raise PdfHasNoTextLayerError(NO_TEXT_LAYER_MESSAGE)
        return UploadFileReport(pages_without_text=empty)
