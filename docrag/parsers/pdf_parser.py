"""PDF parser (pdfplumber + pypdfium2).

Produces a :class:`~docrag.ir.Document` from a PDF, page by page. Three
heuristics compensate for PDF being a layout format rather than a semantic one:

1. **Table extraction** uses pdfplumber's ``page.find_tables()``. Each detected
   table becomes a :class:`~docrag.ir.Element` of kind ``table`` with its
   bounding box stored in :class:`~docrag.ir.SourceRef`, so a chunk can point
   back to the exact region. Cell text is read per-cell from PDFium's
   ``TextPage.get_text_bounded`` (which applies the Unicode bidi algorithm, so
   Persian cell text comes out in *logical* order). pdfplumber gives no merge
   info, so cells carry ``row_span=col_span=1`` (noted as a known limitation).

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

License note
------------
This parser deliberately avoids PyMuPDF (AGPL-3.0), whose network-use clause
would force a commercial online service such as a chatbot to release its whole
source tree. It relies instead on two permissively-licensed libraries:

* **pdfplumber** (MIT) — page geometry, character data and table structure.
* **pypdfium2** (BSD-3-Clause / Apache-2.0, wrapping Google's PDFium which is
  BSD-3-Clause) — text extraction with native bidi reordering for correct
  Persian output.

Neither carries a copyleft or network-use obligation, so the parser is safe to
embed in a closed-source commercial service.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2 as pdfium

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

# PDFium's bidi reverses a leading list/heading marker "N." to ".N" at the
# start of an RTL line (it places the terminator before the number). Restore
# the marker so it reads "۱." / "2.1." and the heading-number regex can fire.
_MARKER_FLIP_RE = re.compile(
    r"^\.\s*([0-9\u0660-\u0669\u06F0-\u06F9]+(?:\.[0-9\u0660-\u0669\u06F0-\u06F9]+)*)(?=\s|$)"
)


class PdfParser(BaseParser):
    file_type = "pdf"

    def parse(self, path: str | Path, *, doc_id: str | None = None) -> Document:
        path = Path(path)
        plb = pdfplumber.open(str(path))
        doc_pdfium = pdfium.PdfDocument(str(path))
        try:
            elements: list[Element] = []
            section_path: list[tuple[int, str]] = []
            body_size = self._modal_body_size(plb)
            # Per-parse heading size buckets (instance attribute, no globals).
            self._heading_buckets = self._heading_buckets(plb, body_size)
            ordinal = 0

            for page_number, page in enumerate(plb.pages, start=1):
                page_w, page_h = page.width, page.height
                fpage = doc_pdfium[page_number - 1]
                tp = fpage.get_textpage()
                table_regions: list[Any] = []

                # ---- tables first so text blocks can skip their regions ---- #
                tables = self._find_tables(page, tp, page_h)
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

                # ---- text blocks in reading order ------------------------- #
                lines = self._build_lines(page, tp, page_h)
                blocks = self._group_blocks(lines)
                ordered = self._reading_order_blocks(blocks, page_w)
                for blk in ordered:
                    if any(_rects_overlap(blk["bbox"], r) for r in table_regions):
                        continue
                    text, dom_size = blk["text"], blk["dom"]
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
            page_count = len(plb.pages)
            meta = self._doc_metadata(plb)
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
                        "title": meta["title"],
                        "author": meta["author"],
                        "body_font_size": round(body_size, 2) if body_size else None,
                        "heading_sizes": [round(s, 2) for s in self._heading_buckets],
                    },
                ),
            )
        finally:
            plb.close()
            doc_pdfium.close()

    # -- font analysis (over all pages, from pdfplumber chars) ------------- #
    def _modal_body_size(self, plb) -> float:
        sizes: list[float] = []
        for page in plb.pages:
            for c in page.chars:
                txt = c.get("text", "")
                if txt.strip() and not _BULLET_CHARS.intersection(txt):
                    sizes.append(float(c.get("size", 0)))
        if not sizes:
            return 0.0
        try:
            return statistics.mode(round(s, 1) for s in sizes)
        except statistics.StatisticsError:
            return statistics.median(sizes)

    def _heading_buckets(self, plb, body_size: float) -> list[float]:
        """Return descending font sizes that count as headings (levels 1..N)."""
        if not body_size:
            return []
        big: list[float] = []
        for page in plb.pages:
            for c in page.chars:
                size = round(float(c.get("size", 0)), 1)
                txt = c.get("text", "").strip()
                if size >= body_size * 1.15 and len(txt) <= 120 and txt:
                    big.append(size)
        if not big:
            return []
        # Bucket to 0.5pt, sort desc, take distinct buckets.
        buckets = sorted({round(s * 2) / 2 for s in big}, reverse=True)
        return buckets[:6]

    # -- tables ----------------------------------------------------------- #
    def _find_tables(self, page, tp, page_h: float) -> list[dict[str, Any]]:
        """Detect tables via pdfplumber and read cell text via PDFium.

        PDFium's ``get_text_bounded`` applies the Unicode bidi algorithm, so
        Persian cell text comes out in logical order (``سال`` not ``لاس``),
        unlike a raw left-to-right char sweep which reverses RTL runs.
        """
        out: list[dict[str, Any]] = []
        try:
            tables = page.find_tables()
        except Exception:
            return out
        for tbl in tables or []:
            bbox = list(tbl.bbox) if tbl.bbox else None
            if bbox is None:
                continue
            rows = self._table_rows(tbl, tp, page_h)
            if not rows:
                continue
            if not any(any(str(c).strip() for c in row) for row in rows):
                continue
            has_header = len(rows) >= 2
            out.append({"bbox": bbox, "rows": rows, "has_header": has_header})
        return out

    def _table_rows(self, tbl, tp, page_h: float) -> list[list[str]]:
        """Build a row-major grid of cell text using each cell's exact bbox."""
        grid: list[list[str]] = []
        for tr in tbl.rows:
            cell_bboxes = getattr(tr, "cells", None) or []
            if not cell_bboxes:
                continue
            row_text: list[str] = []
            for b in cell_bboxes:
                if b is None:
                    row_text.append("")
                    continue
                txt = self._bounded_text(tp, b, page_h).replace("\n", " ")
                row_text.append(txt)
            grid.append(row_text)
        return grid

    # -- lines / blocks (geometry from pdfplumber, text from PDFium) ------ #
    def _build_lines(self, page, tp, page_h: float) -> list[dict[str, Any]]:
        """Group page chars into visual lines; read each line's logical text."""
        chars = page.chars
        if not chars:
            return []
        chars_sorted = sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"]))
        raw_lines: list[list[dict]] = []
        cur: list[dict] = []
        cur_top: float | None = None
        line_tol = 3.0  # points; chars on the same visual line agree to ~1pt
        for c in chars_sorted:
            top = c["top"]
            if cur_top is None or abs(top - cur_top) <= line_tol:
                cur.append(c)
                if cur_top is None:
                    cur_top = top
            else:
                raw_lines.append(cur)
                cur = [c]
                cur_top = top
        if cur:
            raw_lines.append(cur)

        out: list[dict[str, Any]] = []
        for ln in raw_lines:
            nonws = [c for c in ln if c["text"].strip()]
            if not nonws:
                continue
            x0 = min(c["x0"] for c in ln)
            x1 = max(c["x1"] for c in ln)
            top = min(c["top"] for c in ln)
            bot = max(c["bottom"] for c in ln)
            sizes = [
                round(float(c.get("size", 0)), 1)
                for c in nonws
                if not _BULLET_CHARS.intersection(c["text"])
            ]
            dom = self._mode_or_median(sizes)
            text = self._bounded_text(tp, (x0, top, x1, bot), page_h)
            out.append({"bbox": [x0, top, x1, bot], "dom": dom, "text": text})
        return out

    def _group_blocks(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group lines into blocks. A heading-sized line is its own block."""
        if not lines:
            return []
        blocks: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []

        def flush() -> None:
            if current:
                blocks.append(self._make_block(current))
                current.clear()

        for line in lines:
            if self._is_heading_line(line):
                flush()
                blocks.append(self._make_block([line]))
                continue
            if current:
                prev = current[-1]
                gap = line["bbox"][1] - prev["bbox"][3]
                line_h = prev["bbox"][3] - prev["bbox"][1]
                if gap > max(6.0, 0.6 * line_h):
                    flush()
            current.append(line)
        flush()
        return blocks

    def _make_block(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        x0 = min(l["bbox"][0] for l in lines)
        y0 = min(l["bbox"][1] for l in lines)
        x1 = max(l["bbox"][2] for l in lines)
        y1 = max(l["bbox"][3] for l in lines)
        sizes = [l["dom"] for l in lines if l["dom"]]
        dom = self._mode_or_median(sizes)
        text = "\n".join(l["text"] for l in lines)
        return {"bbox": [x0, y0, x1, y1], "dom": dom, "text": text}

    def _is_heading_line(self, line: dict[str, Any]) -> bool:
        buckets = getattr(self, "_heading_buckets", []) or []
        if not buckets or not line["dom"]:
            return False
        if len(line["text"]) > 120:
            return False
        return self._heading_level_for_size(line["dom"]) is not None

    # -- reading order ---------------------------------------------------- #
    def _reading_order_blocks(self, blocks, page_w: float) -> list[dict[str, Any]]:
        if not blocks:
            return []
        split_x = self._column_split(blocks, page_w)
        if split_x is None:
            return sorted(blocks, key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
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
        left = [c for c in centers if c < mid]
        right = [c for c in centers if c >= mid]
        if len(left) < 3 or len(right) < 3:
            return None
        if max(left) < gap_lo and min(right) > gap_hi:
            return (max(left) + min(right)) / 2
        return None

    # -- block → text ----------------------------------------------------- #
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

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _bounded_text(tp, bbox, page_h: float) -> str:
        """Read logical-order text inside ``bbox`` via PDFium.

        pdfplumber bboxes are ``(x0, top, x1, bottom)`` with a top-left origin
        (``top`` grows downward). PDFium's ``get_text_bounded`` takes
        ``(left, bottom, right, top)`` in PDF user space (bottom-left origin),
        so the vertical axis is flipped.
        """
        x0, top, x1, bottom = bbox
        raw = tp.get_text_bounded(x0, page_h - bottom, x1, page_h - top)
        txt = raw.replace("\r\n", "\n").replace("\r", "").strip()
        # Undo PDFium's leading-marker reversal (".۱" → "۱.").
        txt = _MARKER_FLIP_RE.sub(r"\1.", txt)
        return txt

    @staticmethod
    def _mode_or_median(values: list[float]) -> float:
        if not values:
            return 0.0
        try:
            return statistics.mode(round(v, 1) for v in values)
        except statistics.StatisticsError:
            return statistics.median(values)

    @staticmethod
    def _doc_metadata(plb) -> dict[str, str]:
        m = plb.metadata or {}

        def _get(key: str) -> str:
            for mk, mv in m.items():
                if mk.lower().lstrip("/") == key and mv:
                    return str(mv)
            return ""

        return {"title": _get("title"), "author": _get("author")}


def _rects_overlap(a, b, *, tol: float = 1.0) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    # Overlap if both axes intersect by more than tol points.
    ox = min(ax1, bx1) - max(ax0, bx0)
    oy = min(ay1, by1) - max(ay0, by0)
    return ox > tol and oy > tol
