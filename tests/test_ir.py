"""Tests for the Document IR serialisation round-trip."""
from __future__ import annotations

from docrag.ir import (
    Document,
    Element,
    ElementKind,
    Section,
    SourceRef,
    TableCell,
    TableRow,
)


def _sample_doc() -> Document:
    return Document(
        id="sample",
        source_file="sample.docx",
        file_type="docx",
        elements=[
            Element(
                id="sample::p0::heading",
                kind=ElementKind.HEADING,
                source=SourceRef(file="sample.docx", paragraph_index=0),
                text="عنوان",
                level=1,
                section=[],
            ),
            Element(
                id="sample::tbl::0",
                kind=ElementKind.TABLE,
                source=SourceRef(file="sample.docx", paragraph_index=2),
                rows=[
                    TableRow(
                        index=0,
                        is_header=True,
                        cells=[TableCell(text="A", col_span=2), TableCell(text="B")],
                    ),
                    TableRow(
                        index=1,
                        cells=[
                            TableCell(text="x", row_span=2, vmerge="restart"),
                            TableCell(text="y"),
                        ],
                    ),
                ],
            ),
        ],
        sections=[Section(id="s1", title="عنوان", level=1)],
        metadata={"paragraph_count": 2},
    )


def test_roundtrip_preserves_structure():
    doc = _sample_doc()
    d = doc.to_dict()
    back = Document.from_dict(d)
    assert back.id == doc.id
    assert back.file_type == doc.file_type
    assert len(back.elements) == len(doc.elements)
    assert back.elements[0].kind == ElementKind.HEADING
    tbl = back.elements[1]
    assert tbl.kind == ElementKind.TABLE
    assert tbl.rows[0].cells[0].col_span == 2
    assert tbl.rows[1].cells[0].row_span == 2
    assert tbl.rows[1].cells[0].vmerge == "restart"


def test_document_accessors():
    doc = _sample_doc()
    assert len(doc) == 2
    assert sum(1 for _ in doc.tables()) == 1
    assert sum(1 for _ in doc.headings()) == 1
    assert doc.find("sample::tbl::0") is not None
    assert doc.find("nope") is None


def test_iter_with_section_path():
    els = [
        Element(id="h1", kind=ElementKind.HEADING, source=SourceRef(file="x"), text="بخش", level=1),
        Element(id="p1", kind=ElementKind.PARAGRAPH, source=SourceRef(file="x"), text="متن"),
        Element(id="h2", kind=ElementKind.HEADING, source=SourceRef(file="x"), text="زیر", level=2),
        Element(id="p2", kind=ElementKind.PARAGRAPH, source=SourceRef(file="x"), text="متن۲"),
    ]
    paths = [sp for _, sp in __import__("docrag.ir", fromlist=["iter_with_section_path"]).iter_with_section_path(els)]
    # A heading is part of its own section, so its path includes its title.
    assert paths[0] == ["بخش"]
    assert paths[1] == ["بخش"]
    assert paths[2] == ["بخش", "زیر"]
    assert paths[3] == ["بخش", "زیر"]


def test_schema_version_in_dict():
    doc = _sample_doc()
    assert doc.to_dict()["schema_version"] == "0.2"
