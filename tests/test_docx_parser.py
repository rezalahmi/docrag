"""Tests for the DOCX parser against the contract fixture."""
from __future__ import annotations

from collections import Counter

from docrag.parsers.docx_parser import DocxParser


def _parse(contract_docx):
    return DocxParser().parse(contract_docx)


def test_parses_contract_into_expected_kinds(contract_docx):
    doc = _parse(contract_docx)
    counts = Counter(e.kind.value for e in doc.elements)
    assert counts["heading"] >= 5
    assert counts["paragraph"] >= 5
    assert counts["list_item"] >= 4     # 3 numbered + 2 bullets
    assert counts["table"] == 1
    assert counts["footnote"] == 2


def test_list_markers_are_persian_and_sequential(contract_docx):
    doc = _parse(contract_docx)
    ordered = [e for e in doc.elements if e.kind.value == "list_item" and e.ordered]
    markers = [e.list_marker for e in ordered]
    assert markers == ["۱.", "۲.", "۳."]


def test_bullet_items_unordered(contract_docx):
    doc = _parse(contract_docx)
    bullets = [e for e in doc.elements if e.kind.value == "list_item" and not e.ordered]
    assert len(bullets) == 2


def test_headings_carry_level_and_section_path(contract_docx):
    doc = _parse(contract_docx)
    headings = [e for e in doc.elements if e.kind.value == "heading"]
    assert any(h.level == 2 for h in headings)  # "تعاریف" is level 2
    # A heading's own section list is the path down to it; the last body element
    # should sit under a non-empty section path.
    assert any(e.section for e in doc.elements if e.kind.value == "paragraph")


def test_table_has_vertical_and_horizontal_merge(contract_docx):
    doc = _parse(contract_docx)
    tbl = next(e for e in doc.elements if e.kind.value == "table")
    # Vertical merge: some cell has row_span >= 2.
    assert any(c.row_span >= 2 for r in tbl.rows for c in r.cells)
    # Horizontal merge: some cell has col_span >= 2.
    assert any(c.col_span >= 2 for r in tbl.rows for c in r.cells)
    # Continuation positions are NOT emitted (HTML-style rows).
    assert all(c.row_span >= 1 and c.col_span >= 1 for r in tbl.rows for c in r.cells)


def test_footnotes_extracted_with_text(contract_docx):
    doc = _parse(contract_docx)
    fns = [e for e in doc.elements if e.kind.value == "footnote"]
    assert len(fns) == 2
    assert all(f.text.strip() for f in fns)
    assert all(f.meta.get("footnote_id") in (1, 2) for f in fns)


def test_arabic_letters_normalised(contract_docx):
    doc = _parse(contract_docx)
    # The fixture seeded Arabic ي/ك; none should survive into element text.
    joined = " ".join(e.text for e in doc.elements if e.kind.value == "paragraph")
    assert "ي" not in joined  # Arabic yeh
    assert "ك" not in joined  # Arabic kaf


def test_validation_ok_on_contract(contract_docx):
    from docrag.pipeline import Pipeline
    r = Pipeline().run(contract_docx)
    assert r.ok
    assert r.report.error_count == 0
