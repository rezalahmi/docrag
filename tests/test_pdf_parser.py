"""Tests for the PDF parser against the financial report fixture."""
from __future__ import annotations

from collections import Counter

from docrag.parsers.pdf_parser import PdfParser


def _parse(financial_pdf):
    return PdfParser().parse(financial_pdf)


def test_parses_financial_report_into_expected_kinds(financial_pdf):
    doc = _parse(financial_pdf)
    counts = Counter(e.kind.value for e in doc.elements)
    assert counts["heading"] >= 3
    assert counts["paragraph"] >= 5
    assert counts["table"] == 1


def test_table_text_not_reversed(financial_pdf):
    """Persian table headers must come out in logical order, not reversed."""
    doc = _parse(financial_pdf)
    tbl = next(e for e in doc.elements if e.kind.value == "table")
    header_texts = [c.text for c in tbl.rows[0].cells]
    # The report's header row is سال / درآمد / هزینه / سود (in any normalised
    # variant). Reversed extraction would yield لاس/دمارد/دوس instead.
    assert "سال" in header_texts
    assert "سود" in header_texts
    assert "لاس" not in header_texts
    assert "دوس" not in header_texts


def test_table_contains_year_rows(financial_pdf):
    doc = _parse(financial_pdf)
    tbl = next(e for e in doc.elements if e.kind.value == "table")
    body = "\n".join(c.text for r in tbl.rows[1:] for c in r.cells)
    assert "۱۴۰۳" in body
    assert "۱۲٬۰۰۰٬۰۰۰٬۰۰۰" in body or "۱۲,۰۰۰,۰۰۰,۰۰۰" in body


def test_headings_have_levels(financial_pdf):
    doc = _parse(financial_pdf)
    headings = [e for e in doc.elements if e.kind.value == "heading"]
    assert all(1 <= h.level <= 6 for h in headings)


def test_source_refs_carry_page_and_bbox(financial_pdf):
    doc = _parse(financial_pdf)
    for el in doc.elements:
        assert el.source.page is not None
        if el.kind.value == "table":
            assert el.source.bbox is not None
            assert len(el.source.bbox) == 4


def test_validation_ok_on_financial(financial_pdf):
    from docrag.pipeline import Pipeline
    r = Pipeline().run(financial_pdf)
    assert r.ok
    assert r.report.error_count == 0
