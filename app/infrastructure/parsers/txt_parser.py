from app.domain.document_blocks import DocumentBlocksBuilder
from app.domain.interfaces.document_parser import DocumentBlockType, ParsedBlock, ParsedDocument
from app.domain.source_text import has_paragraph_separator, paragraphize
from app.domain.text_normalization import normalize_text
from app.infrastructure.parsers.line_scanner import iter_lines


class TxtParser:
    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        text = self.extract_text(raw_bytes)
        return ParsedDocument(plain_text=text, sections=[], blocks=_split_blocks(text))

    def extract_source_text(self, raw_bytes: bytes) -> str:
        return paragraphize(self.extract_text(raw_bytes))

    def extract_text(self, raw_bytes: bytes) -> str:
        return normalize_text(raw_bytes.decode("utf-8", errors="replace"))


def _split_blocks(text: str) -> list[ParsedBlock]:
    """Абзацы разделяются пустыми строками; если пустых строк нет вовсе,
    блоком считается каждая непустая строка — иначе весь файл стал бы одним блоком."""
    builder = DocumentBlocksBuilder(len(text))
    split_by_lines = not has_paragraph_separator(text)
    start: int | None = None
    end = 0
    for line in iter_lines(text):
        if line.is_blank:
            if start is not None:
                builder.add(DocumentBlockType.PARAGRAPH, start, end)
                start = None
            continue
        if split_by_lines:
            builder.add(DocumentBlockType.PARAGRAPH, line.content_start, line.content_end)
            continue
        if start is None:
            start = line.content_start
        end = line.content_end
    if start is not None:
        builder.add(DocumentBlockType.PARAGRAPH, start, end)
    return builder.build()
