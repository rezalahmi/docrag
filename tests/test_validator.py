"""Tests for the validator."""
from __future__ import annotations

from docrag.chunker import Chunk
from docrag.ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from docrag.validator import validate


def _good_doc() -> Document:
    return Document(
        id="d",
        source_file="d.docx",
        file_type="docx",
        elements=[
            Element(
                id="d::p0",
                kind=ElementKind.PARAGRAPH,
                source=SourceRef(file="d.docx", paragraph_index=0),
                text="متن نمونه",
            )
        ],
    )


def test_valid_doc_passes():
    report = validate(_good_doc())
    assert report.ok
    assert report.error_count == 0


def test_duplicate_id_is_error():
    doc = _good_doc()
    doc.elements.append(doc.elements[0])
    report = validate(doc)
    assert not report.ok
    assert any(i.code == "el.id.duplicate" for i in report.issues)


def test_dangling_chunk_source_is_error():
    doc = _good_doc()
    chunks = [Chunk(id="c1", type="text", text="x", source_elements=["nonexistent"], source=doc.elements[0].source)]
    report = validate(doc, chunks)
    assert not report.ok
    assert any(i.code == "chunk.source.dangling" for i in report.issues)


def test_empty_chunk_text_is_error():
    doc = _good_doc()
    chunks = [Chunk(id="c1", type="text", text="", source_elements=["d::p0"])]
    report = validate(doc, chunks)
    assert any(i.code == "chunk.text.empty" for i in report.issues)


def test_oversize_chunk_is_warning():
    # A chunk genuinely larger than the budget (e.g. an oversized table row
    # the chunker keeps whole) is flagged. We construct one directly to test
    # the validator's oversize rule in isolation.
    doc = _good_doc()
    big_chunk = Chunk(
        id="c1", type="text", text="x" * 5000,
        source_elements=["d::p0"], source=doc.elements[0].source,
        token_count=9999,
    )
    report = validate(doc, [big_chunk], max_tokens=100)
    assert report.warning_count >= 1
    assert any(i.code == "chunk.tokens.oversize" for i in report.issues)


def test_metrics_present():
    doc = _good_doc()
    report = validate(doc, [])
    assert "element_count" in report.metrics
    assert report.metrics["element_count"] == 1


def test_table_empty_rows_flagged():
    doc = Document(
        id="d", source_file="d.docx", file_type="docx",
        elements=[
            Element(
                id="d::tbl::0", kind=ElementKind.TABLE,
                source=SourceRef(file="d.docx", paragraph_index=0),
                rows=[],
            )
        ],
    )
    report = validate(doc)
    assert any(i.code == "el.table.empty" for i in report.issues)
