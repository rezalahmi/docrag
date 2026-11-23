"""Document Intermediate Representation (Document IR).

A file-type-independent, serialisable model of a parsed document. Parsers
(``docrag.parsers``) always produce a :class:`Document`; everything downstream
(chunker, renderer, validator) only consumes this IR and never touches the
original file again. That separation is what lets us re-derive chunks or HTML
without re-parsing, and what makes the IR the single source of truth.

Design notes
------------
* Every :class:`Element` carries a :class:`SourceRef` so any chunk can be
  traced back to a page + bounding box (PDF) or paragraph index (DOCX).
* Tables keep a real grid with ``row_span`` / ``col_span`` / ``vmerge`` so the
  DOCX merge-cell structure survives. PDF tables (pdfplumber gives no merge
  info) fill these with span=1 and ``vmerge=None``.
* ``section`` on each element is the *path* of heading texts from the document
  root down to that element (e.g. ``["گزارش مالی", "عملکرد سالانه"]``). It is
  materialised at parse time so the chunker does not have to walk a tree.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator, Optional


class ElementKind(str, enum.Enum):
    """The kind of an IR element.

    Stored as ``str`` so JSON serialisation is a plain string and the value
    survives a round-trip through ``json.dumps``/``loads`` without custom hooks.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    EQUATION = "equation"
    FOOTNOTE = "footnote"
    TOC_ENTRY = "toc_entry"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"
    CAPTION = "caption"
    UNKNOWN = "unknown"


@dataclass
class SourceRef:
    """Where an element came from inside its source file."""

    file: str
    # PDF: 1-based page number. DOCX: None (paragraph_index is used instead).
    page: Optional[int] = None
    # PDF bounding box in PDF points: [x0, y0, x1, y1].
    bbox: Optional[list[float]] = None
    # DOCX paragraph (or table) ordinal within the document body, 0-based.
    paragraph_index: Optional[int] = None
    # Page width/height in points, captured so a bbox can be rendered to scale.
    page_width: Optional[float] = None
    page_height: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TableCell:
    """One cell of a table grid.

    ``row_span``/``col_span`` mirror the HTML/DOCX merge model. When a cell is
    *covered* by a vertical merge that started above it, ``vmerge`` is
    ``"continue"`` and ``text`` is empty; the originating cell has
    ``vmerge="restart"``. A normal cell has ``vmerge=None``.
    """

    text: str = ""
    row_span: int = 1
    col_span: int = 1
    vmerge: Optional[str] = None  # "restart" | "continue" | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TableRow:
    index: int  # 0-based row position in the table
    cells: list[TableCell] = field(default_factory=list)
    is_header: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "is_header": self.is_header,
            "cells": [c.to_dict() for c in self.cells],
        }


@dataclass
class Element:
    """A single addressable unit of a document."""

    id: str
    kind: ElementKind
    source: SourceRef
    # Section path (list of heading texts) active at this element.
    section: list[str] = field(default_factory=list)
    # For text-like kinds: the (normalised) text. Empty for tables.
    text: str = ""
    # Heading level (1..N) or list nesting level (0..N).
    level: int = 0
    # --- list items ---
    list_marker: str = ""        # e.g. "۱." , "-" , "۲.۱"
    list_index: int = 0          # ordinal within its list, 1-based
    list_id: Optional[str] = None
    ordered: bool = False
    # --- tables ---
    rows: list[TableRow] = field(default_factory=list)
    caption: str = ""
    # --- headings ---
    heading_number: str = ""     # e.g. "۱.۲" auto-detected from text
    # --- arbitrary passthrough metadata (font size, style, flags) ---
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_textual(self) -> bool:
        return self.kind in (
            ElementKind.HEADING,
            ElementKind.PARAGRAPH,
            ElementKind.LIST_ITEM,
            ElementKind.FOOTNOTE,
            ElementKind.CAPTION,
            ElementKind.TOC_ENTRY,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.kind.value,
            "section": list(self.section),
            "text": self.text,
            "level": self.level,
            "list_marker": self.list_marker,
            "list_index": self.list_index,
            "list_id": self.list_id,
            "ordered": self.ordered,
            "rows": [r.to_dict() for r in self.rows],
            "caption": self.caption,
            "heading_number": self.heading_number,
            "source": self.source.to_dict(),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Element":
        src = d.get("source") or {}
        source = SourceRef(
            file=src.get("file", ""),
            page=src.get("page"),
            bbox=src.get("bbox"),
            paragraph_index=src.get("paragraph_index"),
            page_width=src.get("page_width"),
            page_height=src.get("page_height"),
        )
        rows = [
            TableRow(
                index=r.get("index", i),
                is_header=r.get("is_header", False),
                cells=[
                    TableCell(
                        text=c.get("text", ""),
                        row_span=c.get("row_span", 1),
                        col_span=c.get("col_span", 1),
                        vmerge=c.get("vmerge"),
                    )
                    for c in r.get("cells", [])
                ],
            )
            for i, r in enumerate(d.get("rows", []))
        ]
        return cls(
            id=d["id"],
            kind=ElementKind(d.get("type", "unknown")),
            source=source,
            section=list(d.get("section", [])),
            text=d.get("text", ""),
            level=d.get("level", 0),
            list_marker=d.get("list_marker", ""),
            list_index=d.get("list_index", 0),
            list_id=d.get("list_id"),
            ordered=d.get("ordered", False),
            rows=rows,
            caption=d.get("caption", ""),
            heading_number=d.get("heading_number", ""),
            meta=dict(d.get("meta", {})),
        )


@dataclass
class Section:
    """A node in the heading hierarchy (materialised for navigation/render)."""

    id: str
    title: str
    level: int
    number: str = ""
    children: list["Section"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "number": self.number,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Document:
    """The top-level IR object produced by a parser."""

    id: str
    source_file: str
    file_type: str  # "pdf" | "docx"
    elements: list[Element] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- accessors ----
    def by_kind(self, kind: ElementKind) -> Iterator[Element]:
        return (e for e in self.elements if e.kind == kind)

    def tables(self) -> Iterator[Element]:
        return self.by_kind(ElementKind.TABLE)

    def headings(self) -> Iterator[Element]:
        return self.by_kind(ElementKind.HEADING)

    def find(self, element_id: str) -> Optional[Element]:
        for e in self.elements:
            if e.id == element_id:
                return e
        return None

    def __iter__(self) -> Iterator[Element]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)

    # ---- serialisation ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_file": self.source_file,
            "file_type": self.file_type,
            "schema_version": "0.2",
            "metadata": dict(self.metadata),
            "sections": [s.to_dict() for s in self.sections],
            "elements": [e.to_dict() for e in self.elements],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Document":
        elements = [Element.from_dict(e) for e in d.get("elements", [])]

        def _sec(s: dict[str, Any]) -> Section:
            return Section(
                id=s["id"],
                title=s.get("title", ""),
                level=s.get("level", 0),
                number=s.get("number", ""),
                children=[_sec(c) for c in s.get("children", [])],
            )

        sections = [_sec(s) for s in d.get("sections", [])]
        return cls(
            id=d["id"],
            source_file=d.get("source_file", ""),
            file_type=d.get("file_type", ""),
            elements=elements,
            sections=sections,
            metadata=dict(d.get("metadata", {})),
        )


def iter_with_section_path(
    elements: Iterable[Element],
    max_heading_level: int = 6,
) -> Iterator[tuple[Element, list[str]]]:
    """Yield ``(element, section_path)`` while walking ``elements`` in order.

    The section path is rebuilt on the fly from headings of level
    ``<= max_heading_level``. Useful when an element's stored ``section`` is
    stale or when re-deriving chunks from a filtered element list.
    """
    path: list[tuple[int, str]] = []  # stack of (level, title)
    for el in elements:
        if el.kind == ElementKind.HEADING and 0 < el.level <= max_heading_level:
            title = el.text.strip()
            while path and path[-1][0] >= el.level:
                path.pop()
            path.append((el.level, title))
        yield el, [t for _, t in path]
