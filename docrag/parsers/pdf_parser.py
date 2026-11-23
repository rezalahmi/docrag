"""PDF parser (PyMuPDF).

Produces a :class:`~docrag.ir.Document` from a PDF, page by page. Three
heuristics compensate for PDF being a layout format rather than a semantic one:

1. **Table extraction** uses PyMuPDF's built-in ``page.find_tables()``. Each
   detected table becomes a :class:`~docrag.ir.Element` of kind ``table`` with
   its bounding box stored in :class:`~docrag.ir.SourceRef`, so a chunk can
   point back to the exact region. PyMuPDF gives no merge info, so cells carry
   ``row_span=col_span=1`` (this is noted as a known limitation).

2. **Reading order** is approximate. Blocks are first grouped into columns (a
   two-column page is detected from a vertical white river), then sorted
   top-to-bottom within each column. Single-column pages fall through to plain
   vertical order. Text blocks that overlap a detected table region are skipped
   so table content is not double-counted.

3. **Heading detection** is font-size based: the document's modal body size is
   found, then spans clearly larger than body are bucketed into heading levels.
   List items are recognised by a leading marker (``-`` / ``•`` / ``۱.``).

Bounding boxes are kept in PDF points and the page dimensions are stored on
every :class:`~docrag.ir.SourceRef` so an HTML renderer can draw a highlight
rectangle to scale.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any

from ..ids import make_id
from ..ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from ..normalizer import Normalizer
from .base import BaseParser

# A leading list marker: bullet chars, "-" , "–" , or Persian/Arabic/Latin
# digits optionally dotted like "1." / "۱." / "۲.۱.".
_LIST_MARKER_RE = re.compile(
    r"^\s*(?:[•◦●▪–\-*]|\d+(?:\.\d+)*\.?|[\u06F0-\u06F9\u0660-\u0669]+(?:\.[\u06F0-\u06F9\u0660-\u0669]+)*\.?)\s+"
)
_BULLET_CHARS = set("•◦●▪–-*")
_DIGIT_CHARS = set("0123456789۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩.")


class PdfParser(BaseParser):
    file_type = "pdf"

    def parse(self, path: str | Path, *, doc_id: str | None = None) -> Document:
        path = Path(path)
        import fitz

        doc_pdf = fitz.open(str(path))
        elements: list[Element] = []
        section_path: list[tuple[int, str]] = []
        body_size = self._modal_body_size(doc_pdf)
        # Per-parse heading size buckets (instance attribute, no globals).
        self._heading_buckets = self._heading_buckets(doc_pdf, body_size)
        ordinal = 0

        for page_number, page in enumerate(doc_pdf, start=1):
            page_w, page_h = page.rect.width, page.rect.height
            table_regions: list[Any] = []

            # ---- tables first so text blocks can skip their regions -------- #
            tables = self._find_tables(page)
            tbl_ordinal = 0
            for tbl in tables:
                bbox = tbl["bbox"]
                table_regions.append(bbox)
                rows = tbl["rows"]
                if not rows:
                    continue
                ordinal += 1
                tbl_ordinal += 1
                trs = [
                    TableRow(
                        index=i,
                        cells=[TableCell(text=self.normalize(str(cell or ""))) for cell in row],
                        is_header=(i == 0 and tbl.get("has_header", False)),
                    )
                    for i, row in enumerate(rows)
                ]
                el = Element(
                    id=make_id(path.name, f"p{page_number}", "table", str(tbl_ordinal)),
                    kind=ElementKind.TABLE,
                    source=SourceRef(
                        file=path.name,
                        page=page_number,
                        bbox=[round(c, 2) for c in bbox],
                        page_width=page_w,
                        page_height=page_h,
                    ),
                    rows=trs,
                    meta={
                        "row_count": len(trs),
                        "col_count": max((len(r.cells) for r in trs), default=0),
                    },
                )
                el.section = [t for _, t in section_path]
                elements.append(el)

            # ---- text blocks in reading order ------------------------------ #
            blocks = self._reading_order_blocks(page, page_w)
            for blk in blocks:
                if any(_rects_overlap(blk["bbox"], r) for r in table_regions):
                    continue
                text, dom_size = self._block_text(blk)
                if not text.strip():
                    continue
                items = self._split_block(text, dom_size)
                for item in items:
                    ordinal += 1
                    el = self._make_element(
                        item, path, page_number, blk["bbox"], page_w, page_h,
                        ordinal,
                    )
                    if el.kind == ElementKind.HEADING and 0 < el.level <= 6:
                        while section_path and section_path[-1][0] >= el.level:
                            section_path.pop()
                        section_path.append((el.level, el.text.strip()))
                    el.section = [t for _, t in section_path]
                    elements.append(el)

        sections = self._build_sections(path, elements)
        page_count = doc_pdf.page_count
        meta = doc_pdf.metadata or {}
        doc_pdf.close()
        return Document(
            id=doc_id or path.stem,
            source_file=path.name,
            file_type=self.file_type,
            elements=elements,
            sections=sections,
            metadata=self.make_metadata(
                path,
                {
                    "page_count": page_count,
                    "title": meta.get("title", "") or "",
                    "author": meta.get("author", "") or "",
                    "body_font_size": round(body_size, 2) if body_size else None,
                    "heading_sizes": [round(s, 2) for s in self._heading_buckets],
                },
            ),
        )

    # -- font analysis ---------------------------------------------------- #
    def _modal_body_size(self, doc_pdf) -> float:
        sizes: list[float] = []
        for page in doc_pdf:
            for blk in page.get_text("dict")["blocks"]:
                if "lines" not in blk:
                    continue
                for line in blk["lines"]:
                    for span in line["spans"]:
                        txt = span.get("text", "")
                        if txt.strip() and not _BULLET_CHARS.intersection(txt):
                            sizes.append(float(span.get("size", 0)))
        if not sizes:
            return 0.0
        try:
            return statistics.mode(round(s, 1) for s in sizes)
        except statistics.StatisticsError:
            return statistics.median(sizes)

    def _heading_buckets(self, doc_pdf, body_size: float) -> list[float]:
        """Return descending font sizes that count as headings (levels 1..N)."""
        if not body_size:
            return []
        big: list[float] = []
        for page in doc_pdf:
            for blk in page.get_text("dict")["blocks"]:
                if "lines" not in blk:
                    continue
                for line in blk["lines"]:
                    for span in line["spans"]:
                        size = round(float(span.get("size", 0)), 1)
                        txt = span.get("text", "").strip()
                        if size >= body_size * 1.15 and len(txt) <= 120 and txt:
                            big.append(size)
        if not big:
            return []
        # Bucket to 0.5pt, sort desc, take distinct buckets.
        buckets = sorted({round(s * 2) / 2 for s in big}, reverse=True)
        return buckets[:6]

    # -- tables ----------------------------------------------------------- #
    def _find_tables(self, page) -> list[dict[str, Any]]:
        """Detect tables on ``page`` and extract cell text via clip.

        PyMuPDF's ``Table.extract()`` returns Persian cell text in *visual*
        (reversed) order because it does not apply bidi reordering, and for
        some RTL tables it also transposes rows/columns. We therefore ignore
        ``extract()`` for the text and re-extract each cell with
        ``page.get_text("text", clip=bbox)``, which does reorder. Cell bboxes
        come from ``table.rows[r].cells`` (correct row-major order); we fall
        back to ``extract()`` only if that structure is unavailable.
        """
        out: list[dict[str, Any]] = []
        try:
            finder = page.find_tables()
        except Exception:
            return out
        for tbl in getattr(finder, "tables", []) or []:
            bbox = list(tbl.bbox) if tbl.bbox else None
            if bbox is None:
                continue
            rows = self._table_rows_via_clip(page, tbl)
            if not rows:
                continue
            if not any(any(str(c).strip() for c in row) for row in rows):
                continue
            has_header = bool(getattr(tbl, "header", None))
            out.append({"bbox": bbox, "rows": rows, "has_header": has_header})
        return out

    def _table_rows_via_clip(self, page, tbl) -> list[list[str]]:
        """Extract a row-major grid of cell text using per-cell clip regions."""
        import fitz

        tbl_rows = getattr(tbl, "rows", None)
        if tbl_rows:
            grid: list[list[str]] = []
            for tr in tbl_rows:
                cell_bboxes = getattr(tr, "cells", None) or []
                if not cell_bboxes:
                    continue
                row_text: list[str] = []
                for b in cell_bboxes:
                    if b is None:
                        row_text.append("")
                        continue
                    rect = fitz.Rect(b[0], b[1], b[2], b[3])
                    txt = page.get_text("text", clip=rect).strip().replace("\n", " ")
                    row_text.append(txt)
                grid.append(row_text)
            if grid:
                return grid
        # Fallback: extract() (text may be reversed for RTL, but keeps grid).
        try:
            extracted = tbl.extract()
        except Exception:
            return []
        return [[("" if c is None else c) for c in row] for row in extracted]

    # -- reading order ---------------------------------------------------- #
    def _reading_order_blocks(self, page, page_w: float) -> list[dict[str, Any]]:
        raw = page.get_text("dict")["blocks"]
        blocks = [
            {"bbox": b["bbox"], "lines": b["lines"]}
            for b in raw
            if b.get("type", 0) == 0 and "lines" in b
        ]
        if not blocks:
            return []
        split_x = self._column_split(blocks, page_w)
        if split_x is None:
            blocks.sort(key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
            return blocks
        # Two-column: left column first (by y), then right column.
        left = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < split_x]
        right = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 >= split_x]
        left.sort(key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
        right.sort(key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
        return left + right

    @staticmethod
    def _column_split(blocks, page_w: float) -> float | None:
        """Return an x split point if the page looks two-column, else None."""
        if len(blocks) < 6:
            return None
        centers = sorted((b["bbox"][0] + b["bbox"][2]) / 2 for b in blocks)
        mid = statistics.median(centers)
        gap_lo = page_w * 0.35
        gap_hi = page_w * 0.65
        # Distinct left/right populations with a clear gap around mid.
        left = [c for c in centers if c < mid]
        right = [c for c in centers if c >= mid]
        if len(left) < 3 or len(right) < 3:
            return None
        if max(left) < gap_lo and min(right) > gap_hi:
            return (max(left) + min(right)) / 2
        return None

    # -- block → text ----------------------------------------------------- #
    @staticmethod
    def _block_text(blk) -> tuple[str, float]:
        lines: list[str] = []
        sizes: list[float] = []
        for line in blk["lines"]:
            spans = line["spans"]
            line_text = "".join(s.get("text", "") for s in spans)
            if line_text.strip():
                lines.append(line_text)
                for s in spans:
                    if s.get("text", "").strip():
                        sizes.append(float(s.get("size", 0)))
        text = "\n".join(lines)
        dom = statistics.mode(round(s, 1) for s in sizes) if sizes else 0.0
        return text, dom

    def _split_block(self, text: str, dom_size: float) -> list[dict]:
        """Split a block's text into one or more item dicts.

        A block may contain several list lines; each becomes its own item.
        Otherwise the whole block is one paragraph/heading.
        """
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            return []
        # Multi-line block where lines look like list items → split.
        if len(lines) > 1 and sum(bool(_LIST_MARKER_RE.match(ln)) for ln in lines) >= max(2, len(lines) // 2):
            items = []
            for ln in lines:
                if _LIST_MARKER_RE.match(ln):
                    marker = _LIST_MARKER_RE.match(ln).group(0).strip()
                    body = _LIST_MARKER_RE.sub("", ln, count=1)
                    items.append({
                        "kind": "list_item",
                        "text": self.normalize(body),
                        "marker": self.normalize(marker),
                        "size": dom_size,
                    })
                elif items:
                    # continuation of the previous item
                    items[-1]["text"] += " " + self.normalize(ln)
                else:
                    items.append({"kind": "paragraph", "text": self.normalize(ln), "marker": "", "size": dom_size})
            return items
        joined = " ".join(ln.strip() for ln in lines)
        return [{"kind": "block", "text": joined, "marker": "", "size": dom_size}]

    # -- element construction --------------------------------------------- #
    def _make_element(
        self, item, path, page_number, bbox, page_w, page_h, ordinal,
    ) -> Element:
        text = item["text"]
        size = item.get("size", 0.0)
        source = SourceRef(
            file=path.name,
            page=page_number,
            bbox=[round(c, 2) for c in bbox],
            page_width=page_w,
            page_height=page_h,
        )

        # Heading?
        if size and item["kind"] in ("block", "paragraph"):
            level = self._heading_level_for_size(size)
            if level is not None and len(text) <= 120:
                heading_num = self._heading_number(text)
                return Element(
                    id=make_id(path.name, f"p{page_number}", "heading", str(ordinal)),
                    kind=ElementKind.HEADING,
                    source=source,
                    text=self.normalize(text),
                    level=level,
                    heading_number=heading_num,
                    meta={"font_size": round(size, 2)},
                )

        if item["kind"] == "list_item":
            ordered = bool(re.search(r"\d", item["marker"]))
            return Element(
                id=make_id(path.name, f"p{page_number}", "list", str(ordinal)),
                kind=ElementKind.LIST_ITEM,
                source=source,
                text=self.normalize(text),
                list_marker=item["marker"],
                ordered=ordered,
                meta={"font_size": round(size, 2)},
            )

        return Element(
            id=make_id(path.name, f"p{page_number}", "para", str(ordinal)),
            kind=ElementKind.PARAGRAPH,
            source=source,
            text=self.normalize(text),
            meta={"font_size": round(size, 2)},
        )

    def _heading_level_for_size(self, size: float) -> int | None:
        buckets = getattr(self, "_heading_buckets", []) or []
        if not buckets:
            return None
        rounded = round(size * 2) / 2
        for idx, bsize in enumerate(buckets):
            if rounded >= bsize:
                return idx + 1
        return None

    @staticmethod
    def _heading_number(text: str) -> str:
        m = re.match(
            r"^([0-9\u0660-\u0669\u06F0-\u06F9]+(?:\.[0-9\u0660-\u0669\u06F0-\u06F9]+)*)[\.،\)\s]",
            text,
        )
        return m.group(1) if m else ""

    def _build_sections(self, path, elements) -> list:
        from ..ir import Section
        root: list[Section] = []
        stack: list[Section] = []
        for el in elements:
            if el.kind != ElementKind.HEADING or not (0 < el.level <= 6):
                continue
            sec = Section(
                id=make_id(path.name, "sec", el.id),
                title=el.text.strip(),
                level=el.level,
                number=el.heading_number,
            )
            while stack and stack[-1].level >= el.level:
                stack.pop()
            (stack[-1].children if stack else root).append(sec)
            stack.append(sec)
        return root


def _rects_overlap(a, b, *, tol: float = 1.0) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    # Overlap if both axes intersect by more than tol points.
    ox = min(ax1, bx1) - max(ax0, bx0)
    oy = min(ay1, by1) - max(ay0, by0)
    return ox > tol and oy > tol
