from app.domain.exceptions import UnsupportedExportFormatError
from app.domain.interfaces.document_exporter import DocumentExporter
from app.domain.value_objects import DocumentFormatVO
from app.infrastructure.exporters.docx_exporter import DocxExporter
from app.infrastructure.exporters.pdf_annotation_exporter import (
    DEFAULT_ANNOTATION_AUTHOR,
    PdfAnnotationExporter,
)
from app.infrastructure.exporters.text_exporter import TextExporter


class DocumentExporterRegistry:
    def __init__(self, pdf_annotation_author: str = DEFAULT_ANNOTATION_AUTHOR) -> None:
        self._docx_exporter = DocxExporter()
        self._text_exporter = TextExporter()
        self._pdf_exporter = PdfAnnotationExporter(pdf_annotation_author)

    def get_exporter(self, document_format: DocumentFormatVO) -> DocumentExporter:
        if document_format in (DocumentFormatVO.DOCX, DocumentFormatVO.DOC):
            return self._docx_exporter
        if document_format is DocumentFormatVO.PDF:
            return self._pdf_exporter
        if document_format in (DocumentFormatVO.TXT, DocumentFormatVO.MARKDOWN):
            return self._text_exporter
        # Новый формат без своего экспортёра текстовый экспортёр испортил бы молча.
        raise UnsupportedExportFormatError(
            f"Экспорт документов {document_format.value} пока не поддерживается"
        )
