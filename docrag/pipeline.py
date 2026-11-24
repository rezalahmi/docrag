"""Pipeline orchestrator.

Ties the four layers together: parse → chunk → render → validate. Given a file
path it produces a :class:`PipelineResult` and can write the three derived
artifacts (IR JSON, HTML preview, chunks JSON) plus a validation report to a
directory.

The IR JSON is the source of truth; HTML and chunks are derived from it and
from each other only via the IR, never directly from the file. This keeps the
graph acyclic: ``file → IR → {html, chunks}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .chunker import Chunk, SemanticChunker
from .ids import slugify
from .ir import Document
from .normalizer import Normalizer, NormalizerConfig
from .parsers import get_parser
from .renderer import HtmlRenderer
from .validator import ValidationReport, Validator


@dataclass
class PipelineResult:
    """Everything produced for one input file."""

    document: Document
    chunks: list[Chunk] = field(default_factory=list)
    html: str = ""
    report: ValidationReport | None = None
    outputs: dict[str, Path] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report.ok if self.report else True


class Pipeline:
    """Run the full docrag pipeline on a file or directory."""

    def __init__(
        self,
        max_tokens: int = 400,
        normalizer_config: NormalizerConfig | None = None,
        overlap_sentences: int = 0,
    ) -> None:
        self.max_tokens = max_tokens
        self.normalizer = Normalizer(normalizer_config)
        self.chunker = SemanticChunker(
            max_tokens=max_tokens, overlap_sentences=overlap_sentences,
        )
        self.renderer = HtmlRenderer()
        self.validator = Validator(max_tokens=max_tokens)

    # -- run -------------------------------------------------------------- #
    def run(self, path: str | Path) -> PipelineResult:
        # Keep ``path`` as-is: a URL string must not be forced through Path()
        # (it collapses the ``//`` and corrupts the scheme). Each parser casts
        # to Path internally only when it actually needs a filesystem path.
        parser = get_parser(path)
        parser.normalizer = self.normalizer
        doc = parser.parse(path)
        chunks = self.chunker.chunk(doc)
        html = self.renderer.render(doc)
        report = self.validator.validate(doc, chunks)
        return PipelineResult(document=doc, chunks=chunks, html=html, report=report)

    def run_many(self, paths: list[str | Path]) -> list[PipelineResult]:
        return [self.run(p) for p in paths]

    # -- write ------------------------------------------------------------ #
    def write_outputs(self, result: PipelineResult, out_dir: str | Path) -> PipelineResult:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = slugify(result.document.id) or slugify(
            Path(result.document.source_file).stem
        ) or "document"

        ir_path = out_dir / f"{stem}.json"
        ir_path.write_text(
            _dump_json(result.document.to_dict()), encoding="utf-8"
        )

        html_path = out_dir / f"{stem}.html"
        html_path.write_text(result.html, encoding="utf-8")

        chunks_path = out_dir / f"{stem}.chunks.json"
        chunks_path.write_text(
            _dump_json([c.to_dict() for c in result.chunks]), encoding="utf-8"
        )

        report_path = out_dir / f"{stem}.report.json"
        report_path.write_text(
            _dump_json(result.report.to_dict() if result.report else {}), encoding="utf-8"
        )

        result.outputs = {
            "ir": ir_path,
            "html": html_path,
            "chunks": chunks_path,
            "report": report_path,
        }
        return result

    def process(self, path: str | Path, out_dir: str | Path) -> PipelineResult:
        """Run the pipeline and write all artifacts to ``out_dir``."""
        result = self.run(path)
        self.write_outputs(result, out_dir)
        return result

    def process_dir(self, input_dir: str | Path, out_dir: str | Path) -> list[PipelineResult]:
        input_dir = Path(input_dir)
        files = sorted(
            p for p in input_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {
                ".pdf", ".docx", ".xlsx", ".xls", ".html", ".htm",
            }
        )
        results: list[PipelineResult] = []
        for f in files:
            results.append(self.process(f, out_dir))
        return results


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
