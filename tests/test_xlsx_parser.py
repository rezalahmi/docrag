"""XLSX parser → IR structural tests."""
from __future__ import annotations

from docrag import ElementKind, XlsxParser, get_parser


def test_get_parser_dispatches_xlsx(agencies_xlsx):
    assert isinstance(get_parser(agencies_xlsx), XlsxParser)


def test_multi_sheet_detected(agencies_chunks):
    # 2 sheets × (sheet heading + table heading + data rows) → 4 agency rows + 1 contact row
    sheets = {c.section[0] for c in agencies_chunks if c.section}
    assert "اطلاعات نمایندگی‌ها" in sheets
    assert "اطلاعات تماس مرکزی" in sheets


def test_multi_level_header_combined(financial_chunks):
    """Parent + child headers are combined into one flat header per column."""
    table_chunks = [c for c in financial_chunks if c.type == "table"]
    assert table_chunks
    headers = table_chunks[0].text.split("\n\n")[0].replace("ستون‌ها: ", "").split(" | ")
    assert headers == [
        "سال", "درآمد کل", "درآمد عملیاتی",
        "هزینه کل", "هزینه عملیاتی", "سود", "نرخ رشد",
    ]


def test_formula_with_cached_value_used(financial_chunks):
    """سود is a formula; the cached result is returned, not the formula string."""
    chunk = next(c for c in financial_chunks if "سال: ۱۴۰۰" in c.text)
    assert "سود: ۴٬۰۰۰٬۰۰۰٬۰۰۰" in chunk.text
    assert "=" not in chunk.text  # formula string never leaks
    assert "needs_review" not in chunk.meta  # had a cached value


def test_formula_without_cached_value_flagged(financial_chunks):
    """نرخ رشد formulas had their cached value stripped → needs_review."""
    chunk = next(c for c in financial_chunks if "سال: ۱۴۰۱" in c.text)
    assert chunk.meta.get("needs_review") == ["نرخ رشد"]
    assert "نیازمند بررسی" in chunk.text
    assert "=B" not in chunk.text  # formula string never leaks


def test_merged_cell_propagated_to_rows(agencies_chunks):
    """The merged 'منطقه' value is propagated to spanned rows; cell_refs point
    to the anchor cell where the value truly lives."""
    row4 = next(c for c in agencies_chunks if "کد: 102" in c.text)
    row3 = next(c for c in agencies_chunks if "کد: 101" in c.text)
    assert "منطقه: شمال" in row4.text  # propagated
    assert "منطقه: شمال" in row3.text  # anchor
    # cell_ref for منطقه in row 4 points to the anchor A3, not A4.
    assert row4.meta["cell_refs"]["منطقه"] == "A3"
    assert row3.meta["cell_refs"]["منطقه"] == "A3"


def test_phone_and_code_kept_as_exact_string(agencies_chunks):
    """Phone numbers and codes stay exact strings (leading zeros survive)."""
    chunk = next(c for c in agencies_chunks if "کد: 101" in c.text)
    assert "تلفن: 02632550000" in chunk.text
    assert "کد: 101" in chunk.text
    # The 'آ' in 'آدرس' is preserved (Excel display does not fold آ→ا).
    assert "آدرس:" in chunk.text


def test_hidden_row_tracked_not_dropped(agencies_chunks):
    """A hidden row is flagged in meta, not silently dropped."""
    hidden = [c for c in agencies_chunks if c.meta.get("hidden")]
    assert len(hidden) == 1
    assert "نمایندگی شیراز" in hidden[0].text


def test_cell_refs_carry_addresses(agencies_chunks):
    """Every table chunk carries a header → cell-address map."""
    chunk = next(c for c in agencies_chunks if "کد: 101" in c.text)
    refs = chunk.meta["cell_refs"]
    assert refs["تلفن"] == "E3"
    assert refs["کد"] == "B3"
    assert refs["نام نمایندگی"] == "C3"


def test_amounts_formatted_persian_grouped(financial_chunks):
    """Numeric amounts are rendered with Persian digits + the ٬ separator."""
    chunk = next(c for c in financial_chunks if "سال: ۱۴۰۰" in c.text)
    assert "درآمد کل: ۱۰٬۰۰۰٬۰۰۰٬۰۰۰" in chunk.text
    assert "هزینه کل: ۶٬۰۰۰٬۰۰۰٬۰۰۰" in chunk.text


def test_digit_normalisation_matches_latin_query(combined_retriever):
    """A query in Latin digits finds a row whose year is stored as a number."""
    a = combined_retriever.query("سود دوره 1400 چقدر بوده؟")
    assert a.found
    assert "۴٬۰۰۰٬۰۰۰٬۰۰۰" in a.value
