"""Validation layer.

Two jobs:

1. **Structural validation** - check that a :class:`~docrag.ir.Document` (and an
   optional chunk list) is internally consistent: every element has a valid kind
   and a non-empty source pointing at the right file; every chunk's
   ``source_elements`` actually exist; no empty texts where text is required.
2. **Quality report** - aggregate metrics that surface parsing smell at a
   glance: how many elements are empty, how many tables have one row, how many
   chunks exceed the budget, what fraction of IR elements are covered by at
   least one chunk (coverage), heading-depth distribution, etc.

A :class:`ValidationReport` is a list of :class:`Issue` records plus a metrics
block. ``ok`` is True only when there are no ``error``-severity issues.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .chunker import Chunk
from .ir import Document, Element, ElementKind, SourceRef
from .tokens import count_tokens

_VALID_KINDS = {k.value for k in ElementKind}


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    location: str = ""  # element id or "doc" / "chunk:<id>"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    ok: bool = True
    issues: list[Issue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add(self, severity: str, code: str, message: str, location: str = "") -> None:
        self.issues.append(Issue(severity, code, message, location))
        if severity == "error":
            self.ok = False

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [i.to_dict() for i in self.issues],
            "metrics": dict(self.metrics),
        }


class Validator:
    """Validate a :class:`Document` and (optionally) its chunks."""

    def __init__(self, max_tokens: int = 400) -> None:
        self.max_tokens = max_tokens

    # -- public ----------------------------------------------------------- #
    def validate(
        self, doc: Document, chunks: list[Chunk] | None = None,
    ) -> ValidationReport:
        report = ValidationReport()
        self._validate_doc(doc, report)
        if chunks is not None:
            self._validate_chunks(doc, chunks, report)
        report.metrics = self._metrics(doc, chunks)
        return report

    # -- document --------------------------------------------------------- #
    def _validate_doc(self, doc: Document, report: ValidationReport) -> None:
        if not doc.id:
            report.add("error", "doc.id.missing", "Document has no id", "doc")
        if not doc.source_file:
            report.add("error", "doc.source_file.missing", "Document has no source_file", "doc")
        if not doc.elements:
            report.add("warning", "doc.empty", "Document has no elements", "doc")
            return

        seen_ids: set[str] = set()
        for el in doc.elements:
            loc = el.id or "<no-id>"
            if not el.id:
                report.add("error", "el.id.missing", "Element has no id", loc)
            if el.id in seen_ids:
                report.add("error", "el.id.duplicate", f"Duplicate element id {el.id}", loc)
            seen_ids.add(el.id)

            if el.kind.value not in _VALID_KINDS:
                report.add("error", "el.kind.invalid", f"Invalid kind {el.kind!r}", loc)

            self._validate_source(el, doc, report, loc)

            if el.is_textual and not el.text.strip():
                report.add("warning", "el.text.empty", "Textual element has empty text", loc)

            if el.kind == ElementKind.HEADING and not (0 < el.level <= 6):
                report.add("warning", "el.heading.level", f"Heading has odd level {el.level}", loc)

            if el.kind == ElementKind.TABLE:
                if not el.rows:
                    report.add("error", "el.table.empty", "Table has no rows", loc)
                else:
                    for r in el.rows:
                        if not r.cells:
                            report.add("warning", "el.table.empty_row", f"Row {r.index} has no cells", loc)

            # Span sanity: spans must be >= 1.
            if el.kind == ElementKind.TABLE:
                for r in el.rows:
                    for c in r.cells:
                        if c.row_span < 1 or c.col_span < 1:
                            report.add("error", "el.table.span", f"Row {r.index} has span < 1", loc)

    def _validate_source(self, el: Element, doc: Document, report: ValidationReport, loc: str) -> None:
        src: SourceRef = el.source
        if not src.file:
            report.add("error", "src.file.missing", "Source has no file", loc)
        elif src.file != doc.source_file:
            report.add(
                "warning", "src.file.mismatch",
                f"Source file {src.file!r} != doc source {doc.source_file!r}", loc,
            )
        if doc.file_type == "pdf":
            if src.page is None:
                report.add("warning", "src.page.missing", "PDF element has no page", loc)
        elif doc.file_type == "docx":
            if src.paragraph_index is None and el.kind != ElementKind.TABLE:
                report.add("warning", "src.paragraph.missing", "DOCX element has no paragraph_index", loc)
        if src.bbox is not None and len(src.bbox) != 4:
            report.add("error", "src.bbox.shape", f"bbox must have 4 values, got {len(src.bbox)}", loc)

    # -- chunks ----------------------------------------------------------- #
    def _validate_chunks(self, doc: Document, chunks: list[Chunk], report: ValidationReport) -> None:
        if not chunks:
            report.add("warning", "chunks.empty", "No chunks produced", "chunks")
            return
        known_ids = {e.id for e in doc.elements}
        seen: set[str] = set()
        covered: set[str] = set()
        for ch in chunks:
            loc = f"chunk:{ch.id}"
            if not ch.id:
                report.add("error", "chunk.id.missing", "Chunk has no id", loc)
            if ch.id in seen:
                report.add("error", "chunk.id.duplicate", f"Duplicate chunk id {ch.id}", loc)
            seen.add(ch.id)
            if not ch.text.strip():
                report.add("error", "chunk.text.empty", "Chunk has empty text", loc)
            if ch.token_count <= 0:
                report.add("warning", "chunk.tokens.zero", "Chunk token_count <= 0", loc)
            if ch.token_count > self.max_tokens * 1.25:
                report.add(
                    "warning", "chunk.tokens.oversize",
                    f"Chunk {ch.token_count} tokens > budget {self.max_tokens}", loc,
                )
            for eid in ch.source_elements:
                if eid not in known_ids:
                    report.add("error", "chunk.source.dangling",
                               f"Chunk references unknown element {eid}", loc)
                else:
                    covered.add(eid)
            if ch.source is None:
                report.add("warning", "chunk.source.none", "Chunk has no primary source", loc)

        coverage = len(covered) / len(doc.elements) if doc.elements else 0.0
        if coverage < 0.9 and doc.elements:
            report.add(
                "info", "chunks.coverage",
                f"{coverage:.0%} of elements covered by chunks",
                "chunks",
            )

    # -- metrics ---------------------------------------------------------- #
    def _metrics(self, doc: Document, chunks: list[Chunk] | None) -> dict[str, Any]:
        counts: dict[str, int] = {}
        empty_text = 0
        heading_levels: dict[int, int] = {}
        table_row_counts: list[int] = []
        for el in doc.elements:
            counts[el.kind.value] = counts.get(el.kind.value, 0) + 1
            if el.is_textual and not el.text.strip():
                empty_text += 1
            if el.kind == ElementKind.HEADING:
                heading_levels[el.level] = heading_levels.get(el.level, 0) + 1
            if el.kind == ElementKind.TABLE:
                table_row_counts.append(len(el.rows))
        metrics: dict[str, Any] = {
            "element_count": len(doc.elements),
            "element_counts": counts,
            "empty_textual_elements": empty_text,
            "heading_level_distribution": heading_levels,
            "tables": len(table_row_counts),
            "table_row_counts": table_row_counts,
            "section_count": len(doc.sections),
        }
        if chunks is not None:
            token_counts = [c.token_count for c in chunks]
            covered = {eid for c in chunks for eid in c.source_elements}
            metrics.update({
                "chunk_count": len(chunks),
                "chunk_tokens_min": min(token_counts) if token_counts else 0,
                "chunk_tokens_max": max(token_counts) if token_counts else 0,
                "chunk_tokens_mean": round(sum(token_counts) / len(token_counts), 1) if token_counts else 0,
                "chunk_coverage": round(len(covered) / len(doc.elements), 3) if doc.elements else 0.0,
                "chunk_type_counts": _chunk_type_counts(chunks),
            })
        return metrics


def _chunk_type_counts(chunks: Iterable[Chunk]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in chunks:
        out[c.type] = out.get(c.type, 0) + 1
    return out


def validate(doc: Document, chunks: list[Chunk] | None = None, *, max_tokens: int = 400) -> ValidationReport:
    """Validate ``doc`` (and ``chunks``) and return a :class:`ValidationReport`."""
    return Validator(max_tokens=max_tokens).validate(doc, chunks)
