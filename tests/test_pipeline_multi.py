"""Pipeline multi-source tests: PDF, DOCX, XLSX and URL through one pipeline."""
from __future__ import annotations

from pathlib import Path

import pytest

from docrag import Pipeline, get_parser
from docrag.parsers import DocxParser, PdfParser, UrlParser, XlsxParser


def test_get_parser_dispatches_all_four_types(fixtures_dir):
    assert isinstance(get_parser(fixtures_dir / "contract.docx"), DocxParser)
    assert isinstance(get_parser(fixtures_dir / "financial_report.pdf"), PdfParser)
    assert isinstance(get_parser(fixtures_dir / "نمایندگی‌ها.xlsx"), XlsxParser)
    assert isinstance(get_parser(fixtures_dir / "sample_page.html"), UrlParser)


def test_pipeline_runs_docx(contract_docx):
    r = Pipeline().run(contract_docx)
    assert r.document.file_type == "docx"
    assert r.chunks
    assert r.report is not None


def test_pipeline_runs_pdf(financial_pdf):
    r = Pipeline().run(financial_pdf)
    assert r.document.file_type == "pdf"
    assert r.chunks


def test_pipeline_runs_xlsx(agencies_xlsx):
    r = Pipeline().run(agencies_xlsx)
    assert r.document.file_type == "xlsx"
    assert r.chunks
    # Excel row chunks carry cell-level citation.
    table_chunks = [c for c in r.chunks if c.type == "table"]
    assert table_chunks
    assert "cell_refs" in table_chunks[0].meta


def test_pipeline_runs_url(sample_html):
    r = Pipeline().run(sample_html)
    assert r.document.file_type == "url"
    assert r.chunks
    assert r.document.metadata.get("source_url") == str(sample_html)


def test_pipeline_writes_artifacts_for_xlsx(agencies_xlsx, tmp_path):
    r = Pipeline().process(agencies_xlsx, tmp_path)
    assert Path(r.outputs["ir"]).exists()
    assert Path(r.outputs["html"]).exists()
    assert Path(r.outputs["chunks"]).exists()
    assert Path(r.outputs["report"]).exists()
    # The IR JSON round-trips.
    import json
    ir = json.loads(Path(r.outputs["ir"]).read_text(encoding="utf-8"))
    assert ir["file_type"] == "xlsx"
    assert ir["elements"]


def test_process_dir_picks_up_xlsx_and_html(fixtures_dir, tmp_path):
    """process_dir now ingests xlsx and html alongside pdf/docx."""
    results = Pipeline().process_dir(fixtures_dir, tmp_path)
    types = {r.document.file_type for r in results}
    assert "docx" in types
    assert "pdf" in types
    assert "xlsx" in types
    assert "url" in types


def test_unified_chunk_store_has_all_types(fixtures_dir):
    """A single Retriever can be built over chunks from every source type."""
    from docrag import Retriever
    p = Pipeline()
    chunks = []
    for name in ["contract.docx", "financial_report.pdf",
                 "نمایندگی‌ها.xlsx", "صورت_مالی.xlsx", "sample_page.html"]:
        chunks.extend(p.run(fixtures_dir / name).chunks)
    assert chunks
    # All five files contributed at least one chunk.
    sources = {c.source.file for c in chunks if c.source}
    assert len(sources) >= 5
    ret = Retriever(chunks)
    # An Excel exact-value question still resolves in the mixed store.
    a = ret.query("سود دوره ۱۴۰۰ چقدر بوده؟")
    assert a.found and a.value == "۴٬۰۰۰٬۰۰۰٬۰۰۰"
    assert a.source["filename"] == "صورت_مالی.xlsx"
    # Header-collision regression: the PDF has an exact "درآمد" column while the
    # XLSX has "درآمد کل". The retriever must still answer from the XLSX cell,
    # not fall back to the "سال" field.
    a2 = ret.query("درآمد سال ۱۴۰۱ چقدر بوده؟")
    assert a2.found and a2.field == "درآمد کل"
    assert a2.value == "۱۲٬۰۰۰٬۰۰۰٬۰۰۰"
    assert a2.source["cell"] == "B5"
