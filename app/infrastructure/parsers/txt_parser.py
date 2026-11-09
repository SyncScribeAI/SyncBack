from app.domain.document_blocks import DocumentBlocksBuilder
from app.domain.interfaces.document_parser import DocumentBlockType, ParsedBlock, ParsedDocument
from app.domain.text_normalization import normalize_text
from app.infrastructure.parsers.line_scanner import iter_lines


class TxtParser:
    def parse(self, raw_bytes: bytes) -> ParsedDocument:
        text = self.extract_text(raw_bytes)
        return ParsedDocument(plain_text=text, sections=[], blocks=_split_blocks(text))

    def extract_text(self, raw_bytes: bytes) -> str:
        return normalize_text(raw_bytes.decode("utf-8", errors="replace"))


def _split_blocks(text: str) -> list[ParsedBlock]:
    """Абзацы разделяются пустыми строками; если пустых строк нет вовсе,
    блоком считается каждая непустая строка — иначе весь файл стал бы одним блоком."""
    builder = DocumentBlocksBuilder(len(text))
    split_by_lines = not _has_blank_separator(text)
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


def _has_blank_separator(text: str) -> bool:
    """Пустая строка из одних пробелов между непустыми тоже разделяет абзацы."""
    seen_content = False
    pending_blank = False
    for line in iter_lines(text):
        if line.is_blank:
            pending_blank = seen_content
        elif pending_blank:
            return True
        else:
            seen_content = True
    return False
