from app.domain.exceptions import UnsupportedExportFormatError
from app.domain.interfaces.document_exporter import DocumentExporter
from app.domain.value_objects import DocumentFormatVO
from app.infrastructure.exporters.docx_exporter import DocxExporter
from app.infrastructure.exporters.text_exporter import TextExporter


class DocumentExporterRegistry:
    def __init__(self) -> None:
        self._docx_exporter = DocxExporter()
        self._text_exporter = TextExporter()

    def get_exporter(self, document_format: DocumentFormatVO) -> DocumentExporter:
        if document_format in (DocumentFormatVO.DOCX, DocumentFormatVO.DOC):
            return self._docx_exporter
        if document_format in (DocumentFormatVO.TXT, DocumentFormatVO.MARKDOWN):
            return self._text_exporter
        # Текстовый экспортёр принял бы байты PDF за текст и вернул бы испорченный файл.
        raise UnsupportedExportFormatError(
            f"Экспорт документов {document_format.value} пока не поддерживается"
        )
