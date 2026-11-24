"""Acceptance retriever tests: the six questions that motivated the Excel
parser, now answered through the unified docrag chunk store."""
from __future__ import annotations


def test_q1_phone_of_agency(combined_retriever):
    a = combined_retriever.query("شماره تلفن نمایندگی البرز چیست؟")
    assert a.found
    assert a.field == "تلفن"
    assert a.value == "02632550000"
    assert a.source["cell"] == "E3"
    assert a.source["sheet"] == "اطلاعات نمایندگی‌ها"


def test_q2_profit_period_1400(combined_retriever):
    a = combined_retriever.query("سود دوره ۱۴۰۰ چقدر بوده؟")
    assert a.found
    assert a.field == "سود"
    assert a.value == "۴٬۰۰۰٬۰۰۰٬۰۰۰"
    assert a.source["cell"] == "F4"
    assert not a.needs_review  # the formula HAD a cached value


def test_q3_income_year_1401(combined_retriever):
    a = combined_retriever.query("درآمد سال ۱۴۰۱ چقدر بوده؟")
    assert a.found
    assert a.field == "درآمد کل"
    assert a.value == "۱۲٬۰۰۰٬۰۰۰٬۰۰۰"
    assert a.source["cell"] == "B5"


def test_q4_address_of_agency(combined_retriever):
    a = combined_retriever.query("آدرس نمایندگی اصفهان چیست؟")
    assert a.found
    assert a.field == "آدرس"
    assert "اصفهان" in a.value
    assert a.source["cell"] == "F5"


def test_q5_which_sheet_has_agencies(combined_retriever):
    a = combined_retriever.query("کدام Sheet اطلاعات نمایندگی‌ها را دارد؟")
    assert a.found
    assert a.field == "sheet"
    assert a.value == "اطلاعات نمایندگی‌ها"


def test_q6_profit_1400_in_which_file(combined_retriever):
    a = combined_retriever.query("سود سال ۱۴۰۰ در کدام فایل ثبت شده؟")
    assert a.found
    assert a.source["filename"] == "صورت_مالی.xlsx"
    assert a.source["cell"] == "F4"
    assert "صورت_مالی.xlsx" in a.value
    assert "F4" in a.value


def test_needs_review_answer_flagged(combined_retriever):
    """A formula without a cached value is flagged, not fabricated."""
    a = combined_retriever.query("نرخ رشد سال ۱۴۰۱ چقدر بوده؟")
    assert a.found
    assert a.needs_review is True
    assert "نیازمند بررسی" in a.value


def test_digit_normalisation_persian_and_latin(combined_retriever):
    """Persian-digit and Latin-digit queries resolve to the same row."""
    fa = combined_retriever.query("سود دوره ۱۴۰۰ چقدر بوده؟")
    en = combined_retriever.query("سود دوره 1400 چقدر بوده؟")
    assert fa.found and en.found
    assert fa.source["cell"] == en.source["cell"] == "F4"
