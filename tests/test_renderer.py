"""Tests for the HTML renderer."""
from __future__ import annotations

from docrag.ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from docrag.renderer import render_html


def _doc() -> Document:
    return Document(
        id="d",
        source_file="d.docx",
        file_type="docx",
        elements=[
            Element(id="d::h0", kind=ElementKind.HEADING, source=SourceRef(file="d.docx", paragraph_index=0), text="عنوان", level=1),
            Element(id="d::p1", kind=ElementKind.PARAGRAPH, source=SourceRef(file="d.docx", paragraph_index=1), text="پاراگراف نمونه"),
            Element(id="d::l2", kind=ElementKind.LIST_ITEM, source=SourceRef(file="d.docx", paragraph_index=2), text="گزینه", list_marker="۱."),
            Element(
                id="d::tbl::0", kind=ElementKind.TABLE,
                source=SourceRef(file="d.docx", paragraph_index=3),
                rows=[
                    TableRow(index=0, is_header=True, cells=[TableCell(text="A"), TableCell(text="B")]),
                    TableRow(index=1, cells=[TableCell(text="x", row_span=1, col_span=2)]),
                ],
            ),
            Element(id="d::fn3", kind=ElementKind.FOOTNOTE, source=SourceRef(file="d.docx", paragraph_index=4), text="تبصره"),
        ],
    )


def test_html_contains_rtl_and_title():
    html = render_html(_doc())
    assert 'dir="rtl"' in html
    assert "d.docx" in html


def test_html_contains_each_element_kind():
    html = render_html(_doc())
    assert "عنوان" in html
    assert "پاراگراف نمونه" in html
    assert "۱." in html
    assert "تبصره" in html
    assert "<table" in html


def test_table_rowspan_colspan_rendered():
    html = render_html(_doc())
    assert 'colspan="2"' in html
    assert "A" in html and "B" in html and "x" in html


def test_source_badge_present():
    html = render_html(_doc())
    assert "src-badge" in html
    assert "d::p1" in html


def test_html_is_self_contained():
    html = render_html(_doc())
    assert "<style>" in html
    assert html.startswith("<!DOCTYPE html>")
