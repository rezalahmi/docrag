"""docrag: Document preprocessing pipeline for Persian RAG.

The package is organised in four layers (see README for the full picture):

* **Parser Layer**  - ``docrag.parsers`` turns a PDF / DOCX / XLSX / URL into a
  file-type-independent :class:`~docrag.ir.Document` (the "Document IR").
* **Document IR**   - :mod:`docrag.ir` defines the structured document model
  (sections, paragraphs, lists, tables, equations, footnotes, source refs).
* **Semantic Chunker** - :mod:`docrag.chunker` derives embedding-ready chunks
  from the IR, one strategy per element type, never blindly splitting a row.
* **Evaluation Layer** - :mod:`docrag.validator` checks schema integrity,
  dangling source references and reports parsing-quality metrics.

A small :class:`~docrag.retriever.Retriever` is included to acceptance-test that
the chunks carry enough structure for exact-value answers.

The IR JSON is the single source of truth; HTML and chunks are derived from it.
"""

from __future__ import annotations

from .chunker import Chunk, SemanticChunker
from .ir import (
    Document,
    Element,
    ElementKind,
    Section,
    SourceRef,
    TableCell,
    TableRow,
)
from .normalizer import Normalizer, NormalizerConfig, normalize
from .parsers import (
    BaseParser,
    DocxParser,
    PdfParser,
    UrlParser,
    XlsxParser,
    get_parser,
)
from .pipeline import Pipeline, PipelineResult
from .retriever import Answer, Retriever

__version__ = "0.3.0"

__all__ = [
    "Document",
    "Element",
    "ElementKind",
    "Section",
    "SourceRef",
    "TableCell",
    "TableRow",
    "Normalizer",
    "NormalizerConfig",
    "normalize",
    "Pipeline",
    "PipelineResult",
    "Chunk",
    "SemanticChunker",
    "BaseParser",
    "DocxParser",
    "PdfParser",
    "XlsxParser",
    "UrlParser",
    "get_parser",
    "Retriever",
    "Answer",
    "__version__",
]

