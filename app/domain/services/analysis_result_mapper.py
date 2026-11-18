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
(как у блоков редактора). Место modify/delete ищет строгий локатор (edit_locator) только
внутри своего блока: если old_text встречается в блоке несколько раз, нужное вхождение
выбирают контекст до и после фрагмента и номер вхождения из ответа. Без них неоднозначная
правка отбрасывается, а не ставится на первое или очередное вхождение. Исключение одно:
одинаковых правок без адреса ровно столько, сколько вхождений, и тогда они покрывают все
вхождения, и порядок раздачи на результат не влияет. Две правки на одно место не
привязываются: вторая отбрасывается. Добавления с общим якорем допустимы: несколько
абзацев после одного якоря — законный ответ.

По каждому исходному предложению маппер оставляет запись в outcomes: привязано или нет, с
причиной и границами. По ним считаются доли корректных и отказанных привязок, поэтому
в outcomes попадают все предложения, а не только сохранённые.

Атрибуции правки к источнику в контракте нет, поэтому источники правки задача анализа
заполняет всеми источниками, ушедшими в запрос.

Сложность O(B + S·L): индекс блоков строится один раз, подстрока ищется только в своём
блоке, а не во всём документе.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.domain.document_blocks import heading_title
from app.domain.document_geometry import PageRect
from app.domain.evidence import Evidence, Quote
from app.domain.interfaces.document_analyzer import (
    AnalysisResult,
    BlockPayload,
    ChangeKind,
    FlagQuote,
    ProposedChange,
)
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.services.edit_locator import (
    Located,
    LocateRefusal,
    TextAnchor,
    locate,
    occurrences,
)
from app.domain.value_objects import SuggestionOriginVO

NO_SECTION = "Без раздела"
# Длина колонки suggestions.section_ref.
_SECTION_REF_MAX = 500
# Длина колонки analysis_flags.reason; причина — код вроде source_conflict, не текст.
_FLAG_REASON_MAX = 64
_KNOWN_KINDS = frozenset(kind.value for kind in ChangeKind)
# Блок, тип, old_text и new_text: правки с одинаковым ключом взаимозаменяемы.
_TwinKey = tuple[str, str, str, str | None]


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
    # Прямоугольники на страницах; заполняются только у PDF, после маппинга.
    geometry: tuple[PageRect, ...] = ()


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
    geometry: tuple[PageRect, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """Исход привязки одного предложения конвейера; тексты не хранятся, только адрес."""

    index: int
    block_ref: str
    kind: str
    placed: bool
    reason: str
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(slots=True)
class MappedResult:
    suggestions: list[SuggestionDraft] = field(default_factory=list)
    flags: list[FlagDraft] = field(default_factory=list)
    # Причина → число отброшенных; для агрегированного предупреждения в логе.
    dropped: dict[str, int] = field(default_factory=dict)
    outcomes: list[ProposalOutcome] = field(default_factory=list)

    def outcome_counts(self) -> dict[str, int]:
        return {
            "proposals": len(self.outcomes),
            "placed": sum(outcome.placed for outcome in self.outcomes),
            "refused": sum(not outcome.placed for outcome in self.outcomes),
        }


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
    taken: dict[tuple[str, str], set[int]] = {}
    twins = _identical_without_address(result.suggestions)
    for number, change in enumerate(result.suggestions):
        draft_or_reason = _to_suggestion(change, index, taken, twins, len(mapped.suggestions))
        if isinstance(draft_or_reason, str):
            mapped.dropped[draft_or_reason] = mapped.dropped.get(draft_or_reason, 0) + 1
            mapped.outcomes.append(
                ProposalOutcome(number, change.block_ref, change.kind, False, draft_or_reason)
            )
        else:
            mapped.suggestions.append(draft_or_reason)
            mapped.outcomes.append(
                ProposalOutcome(
                    number,
                    change.block_ref,
                    draft_or_reason.change_type.value,
                    True,
                    "placed",
                    draft_or_reason.start_offset,
                    draft_or_reason.end_offset,
                )
            )
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
    taken: dict[tuple[str, str], set[int]],
    twins: dict[_TwinKey, int],
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
        found_at = _anchor_position(context.block.text, change, original)
        if found_at < 0:
            return "anchor_not_in_block"
    else:
        original = change.old_text or ""
        key = (change.block_ref, original)
        used = taken.get(key, set())
        located = locate(context.block.text, _anchor(change, original), used)
        if located is LocateRefusal.AMBIGUOUS:
            located = _spread_twins(context.block.text, change, original, used, twins)
        if not isinstance(located, Located):
            return located.value
        found_at = located.start
        taken.setdefault(key, set()).add(found_at)
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


def _has_address(change: ProposedChange) -> bool:
    return bool(change.context_before or change.context_after) or change.occurrence is not None


def _twin_key(change: ProposedChange) -> _TwinKey:
    return (change.block_ref, change.kind, change.old_text or "", change.new_text)


def _identical_without_address(changes: tuple[ProposedChange, ...]) -> dict[_TwinKey, int]:
    counts: dict[_TwinKey, int] = {}
    for change in changes:
        if not _has_address(change):
            key = _twin_key(change)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _spread_twins(
    text: str,
    change: ProposedChange,
    original: str,
    used: set[int],
    twins: dict[_TwinKey, int],
) -> Located | LocateRefusal:
    """Очередное свободное вхождение, если одинаковые правки покрывают все вхождения."""
    if _has_address(change):
        return LocateRefusal.AMBIGUOUS
    starts = occurrences(text, original)
    if twins.get(_twin_key(change), 0) != len(starts):
        return LocateRefusal.AMBIGUOUS
    free = next((start for start in starts if start not in used), None)
    return LocateRefusal.TAKEN if free is None else Located(free)


def _anchor(change: ProposedChange, exact: str) -> TextAnchor:
    return TextAnchor(
        exact=exact,
        prefix=change.context_before or None,
        suffix=change.context_after or None,
        occurrence=change.occurrence,
    )


def _anchor_position(text: str, change: ProposedChange, anchor_text: str) -> int:
    """Якорь добавления: с адресом из ответа строго, без адреса первое вхождение.

    В DOCX новый абзац встаёт после абзаца якоря, и выбор вхождения внутри блока на
    место вставки не влияет; адрес нужен текстовым форматам, где вставка идёт по смещению.
    """
    if change.context_before or change.context_after or change.occurrence is not None:
        located = locate(text, _anchor(change, anchor_text))
        return located.start if isinstance(located, Located) else -1
    return text.find(anchor_text)


def _confidence(value: float | None) -> float | None:
    """[0, 1] с тремя знаками — под колонку Numeric(4,3); NaN и не-числа — как отсутствие."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != value:
        return None
    return round(min(max(float(value), 0.0), 1.0), 3)
