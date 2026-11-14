"""Перевод результата ML-конвейера в черновики правок и флагов.

Контракт не гарантирует, что правка применима, поэтому каждая проверяется по блоку,
на который ссылается:
- блок существует и editable — иначе экспорт не сможет её применить или сломает структуру;
- modify/delete: old_text непустой и дословно входит в текст блока;
- add: якорь (anchor_text) непустой и дословно входит в текст блока; якорь сохраняется
  в original_text — так его ждут экспортёры и фронтенд;
- modify с пустым new_text — это удаление, как и в прежнем маппере.
Неприменимая правка отбрасывается и учитывается в dropped по причине: лучше потерять одну
правку, чем весь результат. Тексты в dropped не попадают — их нельзя писать в логи.

section_ref — название ближайшего предшествующего заголовка или «Без раздела»; position —
порядковый номер среди сохранённых правок, в порядке ответа конвейера.

start_offset/end_offset — границы original_text в plain_text документа, в символах Python
(как у блоков редактора). Если в блоке несколько правок modify/delete с одним old_text,
вхождения раздаются по порядку ответа: курсор по (блок, old_text) сдвигается за найденное
вхождение. Правка сверх числа вхождений отбрасывается — иначе две правки легли бы на
одно место. Добавления с общим якорем курсор не сдвигают: несколько абзацев после одного
якоря — законный ответ.

Атрибуции правки к источнику в контракте нет, поэтому источники правки задача анализа
заполняет всеми источниками, ушедшими в запрос.

Сложность O(B + S·L): индекс блоков строится один раз, подстрока ищется только в своём
блоке, а не во всём документе.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.domain.document_blocks import heading_title
from app.domain.evidence import Evidence, Quote
from app.domain.interfaces.document_analyzer import (
    AnalysisResult,
    BlockPayload,
    ChangeKind,
    FlagQuote,
    ProposedChange,
)
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.value_objects import SuggestionOriginVO

NO_SECTION = "Без раздела"
# Длина колонки suggestions.section_ref.
_SECTION_REF_MAX = 500
# Длина колонки analysis_flags.reason; причина — код вроде source_conflict, не текст.
_FLAG_REASON_MAX = 64
_KNOWN_KINDS = frozenset(kind.value for kind in ChangeKind)


@dataclass(frozen=True, slots=True)
class SuggestionDraft:
    block_ref: str
    section_ref: str
    change_type: ChangeKind
    original_text: str
    suggested_text: str | None
    rationale: str | None
    confidence_score: float | None
    position: int
    start_offset: int
    end_offset: int
    group_key: str | None = None
    origin: SuggestionOriginVO = SuggestionOriginVO.MODEL
    quotes: tuple[Quote, ...] = ()
    # Заполняет локатор оснований после маппинга: ему нужны тексты источников.
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True)
class FlagDraft:
    block_ref: str
    section_ref: str
    reason: str
    original_text: str
    rationale: str | None
    quotes: tuple[FlagQuote, ...]
    position: int
    evidence: tuple[Evidence, ...] = ()


@dataclass(slots=True)
class MappedResult:
    suggestions: list[SuggestionDraft] = field(default_factory=list)
    flags: list[FlagDraft] = field(default_factory=list)
    # Причина → число отброшенных; для агрегированного предупреждения в логе.
    dropped: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BlockContext:
    block: BlockPayload
    section_ref: str


def map_result(
    result: AnalysisResult, blocks: Iterable[BlockPayload], *, max_flags: int | None = None
) -> MappedResult:
    """max_flags — сколько флагов сохранить; остальные учитываются в dropped как flags_over_limit.

    Флаги целиком уходят в агрегат редактора, и лимит держит его размер ограниченным.
    """
    index = index_blocks(blocks)
    mapped = MappedResult()
    cursors: dict[tuple[str, str], int] = {}
    for change in result.suggestions:
        draft_or_reason = _to_suggestion(change, index, cursors, len(mapped.suggestions))
        if isinstance(draft_or_reason, str):
            mapped.dropped[draft_or_reason] = mapped.dropped.get(draft_or_reason, 0) + 1
        else:
            mapped.suggestions.append(draft_or_reason)
    for flag in result.flags:
        if max_flags is not None and len(mapped.flags) >= max_flags:
            mapped.dropped["flags_over_limit"] = mapped.dropped.get("flags_over_limit", 0) + 1
            continue
        context = index.get(flag.block_ref)
        if context is None:
            mapped.dropped["flag_block_not_found"] = (
                mapped.dropped.get("flag_block_not_found", 0) + 1
            )
            continue
        mapped.flags.append(
            FlagDraft(
                block_ref=flag.block_ref,
                section_ref=context.section_ref,
                # Неизвестная причина — тоже повод для ручной проверки, а не для отброса.
                reason=(flag.reason or "unknown")[:_FLAG_REASON_MAX],
                # Текст берём из нашего блока: экспорт и подсветка опираются на него,
                # а не на копию, которую вернул конвейер.
                original_text=context.block.text,
                rationale=flag.rationale,
                quotes=flag.quotes,
                position=len(mapped.flags),
            )
        )
    return mapped


def index_blocks(blocks: Iterable[BlockPayload]) -> dict[str, BlockContext]:
    """ref → блок и его раздел за один проход: раздел — последний встреченный заголовок."""
    index: dict[str, BlockContext] = {}
    section = NO_SECTION
    for block in blocks:
        if block.type is DocumentBlockType.HEADING:
            section = heading_title(block.text)[:_SECTION_REF_MAX] or NO_SECTION
        index[block.ref] = BlockContext(block, section)
    return index


def _to_suggestion(
    change: ProposedChange,
    index: dict[str, BlockContext],
    cursors: dict[tuple[str, str], int],
    position: int,
) -> SuggestionDraft | str:
    """Черновик правки или причина отброса."""
    if change.kind not in _KNOWN_KINDS:
        return "unknown_change_type"
    context = index.get(change.block_ref)
    if context is None:
        return "block_not_found"
    if not context.block.editable:
        return "block_not_editable"
    kind = ChangeKind(change.kind)
    new_text = change.new_text
    if kind is ChangeKind.MODIFY and not (new_text or "").strip():
        kind = ChangeKind.DELETE
    if kind is ChangeKind.DELETE:
        new_text = None
    if kind is ChangeKind.ADD:
        original = change.anchor_text or ""
        if not original.strip():
            return "add_without_anchor"
        if not (new_text or "").strip():
            return "add_without_text"
        found_at = context.block.text.find(original)
        if found_at < 0:
            return "anchor_not_in_block"
    else:
        original = change.old_text or ""
        if not original.strip():
            return "old_text_missing"
        key = (change.block_ref, original)
        cursor = cursors.get(key, 0)
        found_at = context.block.text.find(original, cursor)
        if found_at < 0:
            return "old_text_occurrences_exhausted" if cursor else "old_text_not_in_block"
        cursors[key] = found_at + len(original)
    start = context.block.start_offset + found_at
    return SuggestionDraft(
        block_ref=change.block_ref,
        section_ref=context.section_ref,
        change_type=kind,
        original_text=original,
        suggested_text=new_text,
        rationale=change.rationale,
        confidence_score=_confidence(change.confidence),
        position=position,
        start_offset=start,
        end_offset=start + len(original),
        quotes=change.quotes,
    )


def _confidence(value: float | None) -> float | None:
    """[0, 1] с тремя знаками — под колонку Numeric(4,3); NaN и не-числа — как отсутствие."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != value:
        return None
    return round(min(max(float(value), 0.0), 1.0), 3)
