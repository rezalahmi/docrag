"""Parser layer: turn a source file into a :class:`~docrag.ir.Document`.

Each parser implements :class:`BaseParser` and returns a file-type-independent
IR. Pick a parser with :func:`get_parser` based on the file extension.
"""

from __future__ import annotations

from pathlib import Path

from ..ir import Document
from .base import BaseParser
from .docx_parser import DocxParser
from .pdf_parser import PdfParser

__all__ = ["BaseParser", "DocxParser", "PdfParser", "get_parser"]

_PARSERS = {
    ".docx": DocxParser,
    ".pdf": PdfParser,
}


def get_parser(path: str | Path) -> BaseParser:
    """Return a parser instance for ``path`` based on its extension."""
    suffix = Path(path).suffix.lower()
    cls = _PARSERS.get(suffix)
    if cls is None:
        raise ValueError(
            f"No parser for {suffix!r}. Supported: {sorted(_PARSERS)}"
        )
    return cls()
