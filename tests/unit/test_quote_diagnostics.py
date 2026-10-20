from app.workers.pipeline.quote_diagnostics import diagnose_quote

DOC = "Резервное копирование\nНастройте  расписание\tи хранение."


def test_present_quote():
    assert diagnose_quote("Настройте  расписание", DOC).category == "present"


def test_nbsp_variant():
    result = diagnose_quote("Резервное\u00a0копирование", DOC)
    assert result.category == "invisible_or_space_variants"
    assert result.suspicious_codepoints == ("U+00A0",)


def test_line_endings():
    assert diagnose_quote("Резервное копирование\r\nНастройте", DOC).category == "line_endings"


def test_nfd_quote():
    doc = "caf\u00e9 menu"
    assert diagnose_quote("cafe\u0301 menu", doc).category == "unicode_normalization"


def test_whitespace_runs():
    assert diagnose_quote("Настройте расписание и хранение", DOC).category == "whitespace_runs"


def test_letter_case_is_only_diagnostic():
    assert diagnose_quote("резервное копирование", DOC).category == "letter_case"


def test_paraphrase_is_not_found_with_prefix_length():
    result = diagnose_quote("Резервное копирование данных", DOC)
    assert result.category == "not_found"
    assert result.matched_prefix_len == len("Резервное копирование")
    assert result.quote_len == len("Резервное копирование данных")


def test_prompt_tag_like_text():
    assert diagnose_quote("<source>x", "plain").category == "prompt_tag_like_text"


def test_result_has_no_quote_text():
    result = diagnose_quote("секретная цитата", "другой текст")
    assert "секретная" not in repr(result)
