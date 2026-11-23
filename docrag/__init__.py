"""docrag: Document preprocessing pipeline for Persian RAG.

The package is organised in four layers (see README for the full picture):

* **Parser Layer**  - ``docrag.parsers`` turns a PDF / DOCX file into a
  file-type-independent :class:`~docrag.ir.Document` (the "Document IR").
* **Document IR**   - :mod:`docrag.ir` defines the structured document model
  (sections, paragraphs, lists, tables, equations, footnotes, source refs).
* **Semantic Chunker** - :mod:`docrag.chunker` derives embedding-ready chunks
  from the IR, one strategy per element type, never blindly splitting a row.
* **Evaluation Layer** - :mod:`docrag.validator` checks schema integrity,
  dangling source references and reports parsing-quality metrics.

The IR JSON is the single source of truth; HTML and chunks are derived from it.
"""

from __future__ import annotations

from .ir import (
    Document,
    Element,
    ElementKind,
    SourceRef,
    TableCell,
    TableRow,
)
from .normalizer import Normalizer, normalize
from .pipeline import Pipeline, PipelineResult

__version__ = "0.2.0"

__all__ = [
    "Document",
    "Element",
    "ElementKind",
    "SourceRef",
    "TableCell",
    "TableRow",
    "Normalizer",
    "normalize",
    "Pipeline",
    "PipelineResult",
    "__version__",
]
