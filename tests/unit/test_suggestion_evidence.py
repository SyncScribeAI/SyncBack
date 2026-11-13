"""Основания правок: разбор цитат из обоснования harness и поиск цитат в снимках."""

import io
import json
import uuid
from pathlib import Path

import pytest
from docx import Document as DocxDocument

from app.domain.evidence import (
    Evidence,
    EvidenceMatch,
    EvidenceSource,
    Quote,
    evidence_to_json,
    present_evidence,
)
from app.domain.interfaces.document_analyzer import ChangeKind
from app.domain.services.analysis_result_mapper import FlagDraft, SuggestionDraft
from app.domain.services.evidence_locator import (
    EvidenceLocator,
    LocatedSource,
    attach_evidence,
    fold,
)
from app.domain.value_objects import SuggestionOriginVO
from app.infrastructure.ml_pipeline.rationale_quotes import parse_rationale_quotes
from app.infrastructure.ml_pipeline.schemas import decode_response
from app.infrastructure.parsers.docx_parser import DocxParser

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ml_contract"
MEETING = "Решения встречи от 02.11.2026"


def _source(text: str, name: str = "Регламент", position: int = 0, **kwargs) -> LocatedSource:
    return LocatedSource(
        position=position,
        source_id=kwargs.pop("source_id", str(uuid.uuid4())),
        name=name,
        text=text,
        **kwargs,
    )


# ---------- разбор обоснования ----------


def test_parses_modify_rationale_with_two_quotes() -> None:
    rationale = "Срок изменён. Источник: «Срок — 5 дней.» (Регламент) «Лимит 50 МБ.» (Решения (v2))"
    quotes = parse_rationale_quotes(rationale, ["Регламент", "Решения (v2)"])
    assert quotes == (
        Quote("Регламент", "Срок — 5 дней."),
        Quote("Решения (v2)", "Лимит 50 МБ."),
    )


def test_quote_with_inner_quotes_and_brackets_stays_whole() -> None:
    rationale = (
        "Источник: «Кнопка «Отправить» (справа) активна.» (Руководство) "
        "Проверьте внимательно: модель сомневается."
    )
    assert parse_rationale_quotes(rationale, ["Руководство"]) == (
        Quote("Руководство", "Кнопка «Отправить» (справа) активна."),
    )


def test_parses_add_rationale_with_whole_chunk() -> None:
    rationale = "Новый способ оплаты. Источник (Решения): «Оплата по счёту: да. Срок 3 дня.»"
    assert parse_rationale_quotes(rationale, ["Решения"]) == (
        Quote("Решения", "Оплата по счёту: да. Срок 3 дня."),
    )


def test_unknown_name_is_taken_from_brackets() -> None:
    quotes = parse_rationale_quotes("Источник: «Срок 5 дней.» (chunk.md)", [])
    assert quotes == (Quote("chunk.md", "Срок 5 дней."),)


def test_rationale_without_marker_gives_nothing() -> None:
    assert parse_rationale_quotes("Срок изменён по регламенту.", ["Регламент"]) == ()
    assert parse_rationale_quotes(None, []) == ()


def test_real_fixture_rationales_are_parsed() -> None:
    raw = (FIXTURES / "example_response.json").read_bytes()
    result = decode_response(raw, source_names=[MEETING, "Регламент 2025"])
    assert [change.quotes for change in result.suggestions] == [
        (Quote(MEETING, "Срок ответа на обращение: 5 рабочих дней."),),
        (Quote(MEETING, "Добавлена оплата по счёту для юридических лиц."),),
    ]


def test_structured_quotes_take_priority_over_rationale() -> None:
    raw = json.loads((FIXTURES / "example_response.json").read_text())
    raw["suggestions"][0]["quotes"] = [{"source": "A", "source_id": "s1", "text": "цитата"}]
    result = decode_response(raw, source_names=[MEETING])
    assert result.suggestions[0].quotes == (Quote("A", "цитата", "s1"),)


# ---------- локатор ----------


def test_exact_quote_gets_character_range() -> None:
    text = "# Решения\n\nСрок ответа на обращение: 5 рабочих дней."
    evidence = EvidenceLocator([_source(text)]).locate(Quote("Регламент", "5 рабочих дней"))
    assert evidence.match is EvidenceMatch.EXACT
    assert text[evidence.start : evidence.end] == "5 рабочих дней"
    assert evidence.segment_index == 1


def test_quote_with_other_quotes_dashes_and_spaces_is_exact_after_folding() -> None:
    text = "Срок — «пять»   рабочих\nдней.\n\n- **Лимит** 50 МБ"
    locator = EvidenceLocator([_source(text)])
    evidence = locator.locate(Quote("Регламент", 'срок - "пять" рабочих дней'))
    assert evidence.match is EvidenceMatch.EXACT
    assert text[evidence.start : evidence.end] == "Срок — «пять»   рабочих\nдней"
    markup = locator.locate(Quote("Регламент", "Лимит 50 МБ"))
    assert markup.match is EvidenceMatch.EXACT
    assert text[markup.start : markup.end] == "Лимит** 50 МБ"


def test_paraphrase_highlights_segment() -> None:
    text = "Вступление.\n\nОбращение рассматривается пять рабочих дней с момента регистрации."
    evidence = EvidenceLocator([_source(text)]).locate(
        Quote("Регламент", "рассматривается обращение за пять рабочих дней")
    )
    assert evidence.match is EvidenceMatch.APPROXIMATE
    assert evidence.segment_index == 1
    assert text[evidence.start : evidence.end].startswith("Обращение")


def test_missing_quote_is_not_found_but_keeps_named_source() -> None:
    locator = EvidenceLocator([_source("Совсем другой текст.", position=3)])
    evidence = locator.locate(Quote("Регламент", "Лимит вложений 50 МБ"))
    assert evidence.match is EvidenceMatch.NOT_FOUND
    assert evidence.source_position == 3
    assert evidence.start is None


def test_same_name_takes_source_where_quote_is_found() -> None:
    first = _source("Срок 7 дней.", name="Регламент", position=0)
    second = _source("Срок 5 дней.", name="Регламент", position=1)
    evidence = EvidenceLocator([first, second]).locate(Quote("регламент", "Срок 5 дней"))
    assert (evidence.source_position, evidence.match) == (1, EvidenceMatch.EXACT)


def test_source_id_wins_over_name() -> None:
    first = _source("Срок 5 дней.", name="A", position=0, source_id="a")
    second = _source("Срок 5 дней.", name="B", position=1, source_id="b")
    evidence = EvidenceLocator([first, second]).locate(Quote("A", "Срок 5 дней", "b"))
    assert evidence.source_position == 1


def test_unknown_name_searches_all_sources() -> None:
    sources = [_source("Первый.", name="A"), _source("Срок 5 дней.", name="B", position=1)]
    evidence = EvidenceLocator(sources).locate(Quote("chunk-17", "Срок 5 дней"))
    assert evidence.source_position == 1


def test_fold_maps_positions_back() -> None:
    from array import array

    positions: array[int] = array("I")
    folded = fold("А — «Б»…", positions)
    assert folded == 'а - "б"...'
    assert len(positions) == len(folded)
    assert positions[-1] == 7


# ---------- DOCX и привязка ----------


def _docx() -> bytes:
    document = DocxDocument()
    document.add_heading("Регламент", level=1)
    document.add_paragraph("")
    document.add_paragraph("Срок ответа 5 рабочих дней.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Цена"
    table.cell(0, 1).text = "100 руб."
    table.cell(1, 0).text = "Срок"
    document.add_paragraph("После таблицы.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_layout_keeps_text_and_counts_non_empty_paragraphs() -> None:
    raw = _docx()
    parser = DocxParser()
    layout = parser.extract_source_layout(raw)
    assert layout.text == parser.extract_source_text(raw)
    locators = [dict(span.locator) for span in layout.spans]
    assert locators == [
        {"kind": "docx_paragraphs", "first": 0, "last": 0},
        {"kind": "docx_paragraphs", "first": 1, "last": 1},
        {"kind": "docx_paragraphs", "first": 2, "last": 4},
        {"kind": "docx_paragraphs", "first": 5, "last": 5},
    ]
    for span in layout.spans:
        assert layout.text[span.start : span.end].strip()

    source = _source(layout.text, spans=layout.spans)
    evidence = EvidenceLocator([source]).locate(Quote("Регламент", "100 руб."))
    assert evidence.locator == {"kind": "docx_paragraphs", "first": 2, "last": 4}


def _draft(quotes: tuple[Quote, ...], *, origin=SuggestionOriginVO.MODEL, rationale="r"):
    return SuggestionDraft(
        block_ref="b1",
        section_ref="",
        change_type=ChangeKind.MODIFY,
        original_text="7",
        suggested_text="5",
        rationale=rationale,
        confidence_score=None,
        position=0,
        start_offset=0,
        end_offset=1,
        origin=origin,
        quotes=quotes,
    )


def test_attach_evidence_counts_model_quotes_and_unparsed() -> None:
    locator = EvidenceLocator([_source("Срок 5 дней.")])
    drafts = [
        _draft((Quote("Регламент", "Срок 5 дней"), Quote("Регламент", "нет такого"))),
        _draft((Quote("Регламент", "Срок 5 дней"),), origin=SuggestionOriginVO.PROPAGATED),
        _draft(()),
    ]
    flag = FlagDraft(
        block_ref="b1",
        section_ref="",
        reason="sources_conflict",
        original_text="x",
        rationale=None,
        quotes=(Quote("Регламент", "5 дней"),),
        position=0,
    )
    located, flags, counters = attach_evidence(drafts, [flag], locator)
    assert [item.match for item in located[0].evidence] == [
        EvidenceMatch.EXACT,
        EvidenceMatch.NOT_FOUND,
    ]
    assert located[1].evidence[0].match is EvidenceMatch.EXACT
    assert flags[0].evidence[0].match is EvidenceMatch.EXACT
    assert counters.as_meta() == {
        "evidence_exact": 2,
        "evidence_approximate": 0,
        "evidence_not_found": 1,
        "evidence_unparsed": 1,
    }


@pytest.mark.parametrize(
    ("current_key", "available"), [("files/a.docx", True), ("files/b.docx", False), (None, False)]
)
def test_original_available_only_for_same_file(current_key: str | None, available: bool) -> None:
    source_id = uuid.uuid4()
    rows = evidence_to_json(
        [Evidence(0, "Регламент", "Срок", EvidenceMatch.EXACT, 0, 4, 0)],
        {0: EvidenceSource(0, source_id, "Регламент", uuid.uuid4(), "files/a.docx")},
    )
    keys = {} if current_key is None else {str(source_id): current_key}
    [presented] = present_evidence(rows, keys)
    assert presented["original_file_available"] is available
    assert "file_storage_key" not in presented


def test_fast_fold_matches_char_by_char_fold() -> None:
    import random
    from array import array

    from app.domain.services.evidence_locator import _fold_by_char

    rng = random.Random(7)
    alphabet = list("абвАБЁё «»—–-*`|#> \n\t12.)xyz+")
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        fast, slow = array("I"), array("I")
        assert (fold(text, fast), list(fast)) == (_fold_by_char(text, slow), list(slow))
