"""End-to-end pipeline tests on both fixtures."""
from __future__ import annotations

import json
from pathlib import Path

from docrag.pipeline import Pipeline


def test_process_writes_four_artifacts(contract_docx, tmp_path):
    pipe = Pipeline(max_tokens=300)
    result = pipe.process(contract_docx, tmp_path)
    assert (tmp_path / "contract.json").exists()
    assert (tmp_path / "contract.html").exists()
    assert (tmp_path / "contract.chunks.json").exists()
    assert (tmp_path / "contract.report.json").exists()
    # IR JSON is valid and round-trips.
    ir = json.loads((tmp_path / "contract.json").read_text(encoding="utf-8"))
    assert ir["file_type"] == "docx"
    assert ir["schema_version"] == "0.2"
    # Chunks JSON is a non-empty list.
    chunks = json.loads((tmp_path / "contract.chunks.json").read_text(encoding="utf-8"))
    assert isinstance(chunks, list) and chunks
    assert all("source_elements" in c for c in chunks)
    assert result.ok


def test_chunk_command_roundtrip_from_ir(contract_docx, tmp_path):
    pipe = Pipeline()
    pipe.process(contract_docx, tmp_path)
    ir_path = tmp_path / "contract.json"
    from docrag.chunker import SemanticChunker
    from docrag.ir import Document
    doc = Document.from_dict(json.loads(ir_path.read_text(encoding="utf-8")))
    chunks = SemanticChunker(max_tokens=400).chunk(doc)
    assert chunks
    # Re-chunking the same IR yields the same count deterministically.
    again = SemanticChunker(max_tokens=400).chunk(doc)
    assert len(chunks) == len(again)


def test_pipeline_handles_directory(contract_docx, financial_pdf, tmp_path):
    # Put both fixtures in an input dir and process the whole dir.
    indir = tmp_path / "in"
    indir.mkdir()
    (indir / contract_docx.name).write_bytes(contract_docx.read_bytes())
    (indir / financial_pdf.name).write_bytes(financial_pdf.read_bytes())
    outdir = tmp_path / "out"
    results = Pipeline().process_dir(indir, outdir)
    assert len(results) == 2
    assert all(r.ok for r in results)
    # Two stem-prefix sets of artifacts.
    stems = {p.stem.split(".")[0] for p in outdir.glob("*.json")}
    assert "contract" in stems
    assert "financial_report" in stems


def test_normalizer_config_threads_through_pipeline(contract_docx, tmp_path):
    from docrag.normalizer import NormalizerConfig
    pipe = Pipeline(normalizer_config=NormalizerConfig(digits="latin"))
    result = pipe.run(contract_docx)
    # With digits=latin, Arabic-indic and Persian digits become Latin in text.
    text = " ".join(e.text for e in result.document.elements)
    # The contract has Persian digits like ۴٬۵۰۰٬۰۰۰٬۰۰۰ originally; with latin
    # mode they should appear as ASCII digits.
    assert "4500000000" in text.replace("٬", "").replace(",", "")


def test_chunk_coverage_high(contract_docx, tmp_path):
    pipe = Pipeline()
    result = pipe.run(contract_docx)
    report = result.report
    assert report.metrics["chunk_coverage"] >= 0.9
