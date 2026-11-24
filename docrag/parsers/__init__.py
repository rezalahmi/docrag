"""Parser layer: turn a source file into a :class:`~docrag.ir.Document`.

Each parser implements :class:`BaseParser` and returns a file-type-independent
IR. Pick a parser with :func:`get_parser` based on the file extension or, for
web pages, the URL scheme.

Supported sources:

* ``.pdf``    — :class:`PdfParser`
* ``.docx``   — :class:`DocxParser`
* ``.xlsx``/``.xls`` — :class:`XlsxParser`
* ``http(s)://...`` / ``.html`` / ``.htm`` / ``file://`` — :class:`UrlParser`
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ..ir import Document
from .base import BaseParser
from .docx_parser import DocxParser
from .pdf_parser import PdfParser
from .url_parser import UrlParser
from .xlsx_parser import XlsxParser

__all__ = [
    "BaseParser",
    "DocxParser",
    "PdfParser",
    "XlsxParser",
    "UrlParser",
    "get_parser",
]

_PARSERS = {
    ".docx": DocxParser,
    ".pdf": PdfParser,
    ".xlsx": XlsxParser,
    ".xls": XlsxParser,
    ".html": UrlParser,
    ".htm": UrlParser,
}

_URL_SCHEMES = {"http", "https"}


def get_parser(path: str | Path) -> BaseParser:
    """Return a parser instance for ``path`` based on extension or URL scheme."""
    s = str(path)
    parsed = urlparse(s)
    if parsed.scheme in _URL_SCHEMES:
        return UrlParser()
    suffix = Path(s).suffix.lower()
    cls = _PARSERS.get(suffix)
    if cls is None:
        raise ValueError(
            f"No parser for {suffix!r}. Supported: {sorted(_PARSERS)} "
            f"or an http(s) URL."
        )
    return cls()
