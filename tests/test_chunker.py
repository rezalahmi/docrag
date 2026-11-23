"""Tests for the semantic chunker."""
from __future__ import annotations

from docrag.chunker import SemanticChunker, chunk_document
from docrag.ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow


def _para(i: int, text: str, section=("بخش",)) -> Element:
    return Element(
        id=f"doc::p{i}",
        kind=ElementKind.PARAGRAPH,
        source=SourceRef(file="doc.docx", paragraph_index=i),
        text=text,
        section=list(section),
    )


def _heading(i: int, text: str, level: int) -> Element:
    return Element(
        id=f"doc::h{i}",
        kind=ElementKind.HEADING,
        source=SourceRef(file="doc.docx", paragraph_index=i),
        text=text,
        level=level,
        section=[],
    )


def _doc(elements):
    return Document(id="doc", source_file="doc.docx", file_type="docx", elements=elements)


def test_paragraphs_accumulate_within_budget():
    els = [_para(1, "متن کوتاه اول."), _para(2, "متن کوتاه دوم.")]
    chunks = SemanticChunker(max_tokens=400).chunk(_doc(els))
    # Two short paragraphs should fit in one text chunk.
    text_chunks = [c for c in chunks if c.type == "text" and not c.meta.get("heading")]
    assert any("اول" in c.text and "دوم" in c.text for c in text_chunks)


def test_long_paragraph_split_on_sentences():
    # A paragraph longer than 64-token budget forces a sentence split.
    long = " ".join(f"جمله شماره {i} است." for i in range(60))
    chunks = SemanticChunker(max_tokens=64).chunk(_doc([_para(1, long)]))
    assert len(chunks) > 1
    assert all(c.type == "text" for c in chunks)
    assert all(c.meta.get("split") == "sentence" for c in chunks)


def test_heading_emits_own_chunk_and_sets_section():
    # The parser sets each element's `.section` to its full path; the chunker
    # trusts that and carries it onto chunks. The heading's own section is the
    # path down to it (empty for a top-level heading), the paragraph's includes
    # the heading title.
    heading = _heading(0, "عنوان بخش", 1)  # section=[]
    para = Element(
        id="doc::p1",
        kind=ElementKind.PARAGRAPH,
        source=SourceRef(file="doc.docx", paragraph_index=1),
        text="محتوای بخش.",
        section=["عنوان بخش"],
    )
    chunks = SemanticChunker(max_tokens=400).chunk(_doc([heading, para]))
    assert any(c.meta.get("heading") for c in chunks)
    para_chunks = [c for c in chunks if c.type == "text" and not c.meta.get("heading")]
    assert para_chunks, "expected a paragraph chunk"
    assert para_chunks[0].section == ["عنوان بخش"]


def test_list_items_grouped_and_keep_markers():
    items = [
        Element(
            id=f"doc::l{i}",
            kind=ElementKind.LIST_ITEM,
            source=SourceRef(file="doc.docx", paragraph_index=i),
            text=f"گزینه {i}",
            list_marker=f"{i}.",
            list_id="num1",
            ordered=True,
        )
        for i in range(1, 4)
    ]
    chunks = SemanticChunker(max_tokens=400).chunk(_doc(items))
    list_chunks = [c for c in chunks if c.type == "list"]
    assert len(list_chunks) == 1
    assert list_chunks[0].meta["item_count"] == 3
    assert all(str(i) + "." in list_chunks[0].text for i in range(1, 4))


def test_table_chunk_serialized_as_keyvalue():
    tbl = Element(
        id="doc::tbl::0",
        kind=ElementKind.TABLE,
        source=SourceRef(file="doc.docx", paragraph_index=5),
        rows=[
            TableRow(index=0, is_header=True, cells=[TableCell(text="سال"), TableCell(text="درآمد")]),
            TableRow(index=1, cells=[TableCell(text="۱۴۰۳"), TableCell(text="۵۰۰")]),
        ],
    )
    chunks = SemanticChunker(max_tokens=400).chunk(_doc([tbl]))
    assert len(chunks) == 1 and chunks[0].type == "table"
    txt = chunks[0].text
    assert "سال" in txt and "درآمد" in txt
    assert "۱۴۰۳" in txt and "۵۰۰" in txt
    assert chunks[0].source_elements == ["doc::tbl::0"]


def test_oversize_table_splits_by_rows_with_header_repeated():
    cells = [TableCell(text=f"v{c}") for c in range(5)]
    rows = [TableRow(index=0, is_header=True, cells=[TableCell(text="col")])]
    rows += [TableRow(index=i, cells=[TableCell(text=f"row{i} " + "x" * 200)]) for i in range(1, 6)]
    tbl = Element(
        id="doc::tbl::0",
        kind=ElementKind.TABLE,
        source=SourceRef(file="doc.docx", paragraph_index=5),
        rows=rows,
    )
    chunks = SemanticChunker(max_tokens=120).chunk(_doc([tbl]))
    assert len(chunks) > 1
    # Every part repeats the header line.
    assert all("col" in c.text for c in chunks)
    assert all(c.meta.get("split") == "row" for c in chunks)


def test_footnote_one_chunk():
    fn = Element(
        id="doc::fn1",
        kind=ElementKind.FOOTNOTE,
        source=SourceRef(file="doc.docx", paragraph_index=2),
        text="تبصره نمونه",
        meta={"footnote_id": 1},
    )
    chunks = SemanticChunker(max_tokens=400).chunk(_doc([fn]))
    assert len(chunks) == 1 and chunks[0].type == "footnote"
    assert chunks[0].text == "تبصره نمونه"


def test_chunk_source_traceability():
    els = [_para(1, "متن قابل ردیابی.")]
    chunks = chunk_document(_doc(els), max_tokens=400)
    assert chunks[0].source_elements == ["doc::p1"]
    assert chunks[0].source.file == "doc.docx"
    assert chunks[0].source.paragraph_index == 1
