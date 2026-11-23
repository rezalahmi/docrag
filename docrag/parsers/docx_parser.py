"""DOCX parser.

Walks a .docx body in reading order (paragraphs and tables interleaved) and
produces a :class:`~docrag.ir.Document`. Three things make this parser more
than a thin wrapper around python-docx:

1. **List numbering resolution.** python-docx exposes ``numPr`` (numId + ilvl)
   but *not* the rendered marker (``۱.``, ``۲.۱``, ``-``). We parse
   ``numbering.xml`` ourselves — abstractNum → lvl → numFmt/lvlText/start — and
   keep per-(numId, ilvl) counters with correct restart semantics so a list
   like ``۱. / ۲. / ۲.۱. / ۲.۲. / ۳.`` comes out right.

2. **Merged-cell grid.** python-docx's ``table.cell(r, c)`` returns the cell
   that *covers* a grid position (merged cells share the same ``_tc``). We scan
   the grid once to find each cell's origin + extent, then emit an HTML-style
   row of *origin* cells carrying ``row_span`` / ``col_span`` (and
   ``vmerge="restart"`` for vertically merged cells), so the merge structure is
   preserved without duplicating covered text.

3. **Footnotes.** Footnote references (``w:footnoteReference``) are resolved
   against ``footnotes.xml`` and emitted as :class:`~docrag.ir.Element` of kind
   ``footnote`` right after the paragraph that cites them, preserving order.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from ..ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from ..normalizer import Normalizer
from .base import BaseParser

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

ET.register_namespace("w", W_NS)
ET.register_namespace("r", R_NS)


def qn(tag: str) -> str:
    """Qualify a WordprocessingML tag name, e.g. ``qn("p")`` → ``{w}p``."""
    return f"{{{W_NS}}}{tag}"


# --------------------------------------------------------------------------- #
# Numbering resolution
# --------------------------------------------------------------------------- #


class _LevelDef:
    __slots__ = ("start", "fmt", "lvl_text", "is_bullet")

    def __init__(self, start: int, fmt: str, lvl_text: str) -> None:
        self.start = start
        self.fmt = fmt
        self.lvl_text = lvl_text
        self.is_bullet = fmt == "bullet"


class NumberingResolver:
    """Resolve ``numId`` + ``ilvl`` to a concrete list marker.

    Built from the parsed ``numbering.xml`` part. Stateful: keeps counters so
    consecutive items increment and deeper levels restart correctly.
    """

    _BULLET_FMTS = {"bullet"}

    def __init__(self) -> None:
        # abstractNumId -> {ilvl -> _LevelDef}
        self.abstracts: dict[int, dict[int, _LevelDef]] = {}
        # numId -> abstractNumId
        self.num_to_abstract: dict[int, int] = {}
        # (numId, ilvl) -> current counter value (None = not yet started)
        self.counters: dict[tuple[int, int], int | None] = {}

    @classmethod
    def from_xml(cls, xml_bytes: bytes | None) -> "NumberingResolver":
        self = cls()
        if not xml_bytes:
            return self
        root = ET.fromstring(xml_bytes)
        for an in root.findall(qn("abstractNum")):
            an_id = _to_int(an.get(qn("abstractNumId"))) or 0
            levels: dict[int, _LevelDef] = {}
            for lvl in an.findall(qn("lvl")):
                ilvl = _to_int(lvl.get(qn("ilvl"))) or 0
                start = _first_int(lvl, "start", default=1)
                fmt = _first_text(lvl, "numFmt", default="decimal").lower()
                lvl_text = _first_text(lvl, "lvlText", default="%1.")
                levels[ilvl] = _LevelDef(start=start, fmt=fmt, lvl_text=lvl_text)
            self.abstracts[an_id] = levels
        for num in root.findall(qn("num")):
            num_id = _to_int(num.get(qn("numId"))) or 0
            an_ref = num.find(qn("abstractNumId"))
            if an_ref is not None:
                self.num_to_abstract[num_id] = _to_int(an_ref.get(qn("val"))) or 0
        return self

    # -- public ----------------------------------------------------------- #
    def marker(self, num_id: int, ilvl: int, normalizer: Normalizer) -> tuple[str, bool]:
        """Return ``(marker_text, ordered)`` for one list item.

        Updates internal counters. ``ordered`` is False for bullet levels.
        Returns ``("", False)`` if the numbering definition is unknown.
        """
        abstract_id = self.num_to_abstract.get(num_id)
        if abstract_id is None or abstract_id not in self.abstracts:
            return "", False
        levels = self.abstracts[abstract_id]
        if ilvl not in levels:
            ilvl = min(levels.keys()) if levels else 0
            if ilvl not in levels:
                return "", False
        cur_def = levels[ilvl]

        # Reset deeper levels: a shallower item restarts its descendants.
        for deeper in list(self.counters.keys()):
            if deeper[0] == num_id and deeper[1] > ilvl:
                self.counters[deeper] = None

        key = (num_id, ilvl)
        if self.counters.get(key) is None:
            self.counters[key] = cur_def.start
        else:
            self.counters[key] = self.counters[key] + 1  # type: ignore[operator]

        if cur_def.is_bullet:
            marker = cur_def.lvl_text or "•"
            return normalizer.normalize(marker), False

        # Substitute %1..%9 with the counter at the corresponding level.
        marker = cur_def.lvl_text
        for k in range(1, 10):
            lvl_idx = k - 1
            ph = f"%{k}"
            if ph in marker:
                val = self.counters.get((num_id, lvl_idx))
                if val is None:
                    val = levels[lvl_idx].start if lvl_idx in levels else cur_def.start
                marker = marker.replace(ph, self._format_value(val, levels.get(lvl_idx)))
        return normalizer.normalize(marker), True

    @staticmethod
    def _format_value(value: int, lvl: _LevelDef | None) -> str:
        if lvl is None:
            return str(value)
        fmt = lvl.fmt
        if fmt in ("decimal", "decimalZero"):
            return str(value)
        if fmt == "lowerLetter":
            return _to_alpha(value).lower()
        if fmt == "upperLetter":
            return _to_alpha(value).upper()
        if fmt == "lowerRoman":
            return _to_roman(value).lower()
        if fmt == "upperRoman":
            return _to_roman(value).upper()
        return str(value)


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _first_int(parent: ET.Element, tag: str, *, default: int) -> int:
    el = parent.find(qn(tag))
    if el is None:
        return default
    return _to_int(el.get(qn("val"))) or default


def _first_text(parent: ET.Element, tag: str, *, default: str) -> str:
    el = parent.find(qn(tag))
    if el is None or el.get(qn("val")) is None:
        return default
    return el.get(qn("val")) or default


def _to_alpha(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out or "a"


def _to_roman(n: int) -> str:
    table = [
        (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
        (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
        (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
    ]
    out = ""
    for val, sym in table:
        while n >= val:
            out += sym
            n -= val
    return out or "i"


# --------------------------------------------------------------------------- #
# Heading detection
# --------------------------------------------------------------------------- #

_HEADING_STYLE_RE = re.compile(r"^(?:heading|عنوان)\s*([1-9])$", re.IGNORECASE)
_LEADING_NUM_RE = re.compile(
    r"^\s*((?:\d+[\u0660-\u0669\u06F0-\u06F9]+[\.،]?)|(?:[\u06F0-\u06F9\u0660-\u0669]+(?:\.\d+)*))\s*"
)


def _heading_level(paragraph) -> int | None:
    """Return 1..9 if ``paragraph`` is a heading, else None."""
    style = paragraph.style
    sid = getattr(style, "style_id", "") or ""
    name = getattr(style, "name", "") or ""
    m = _HEADING_STYLE_RE.match(sid) or _HEADING_STYLE_RE.match(name)
    if m:
        return int(m.group(1))
    # outlineLvl in pPr (used by TOC-built headings and some exports)
    pPr = paragraph._p.find(qn("pPr"))
    if pPr is not None:
        outline = pPr.find(qn("outlineLvl"))
        if outline is not None:
            val = _to_int(outline.get(qn("val")))
            if val is not None:
                return val + 1  # outlineLvl is 0-based
    return None


def _num_pr(paragraph) -> tuple[int, int] | None:
    """Return ``(numId, ilvl)`` if the paragraph is a numbered list item.

    Checks the paragraph's own ``numPr`` first, then falls back to the
    paragraph style's ``numPr`` — python-docx's ``List Number`` / ``List Bullet``
    styles carry the numbering there, not on each paragraph.
    """
    num_id, ilvl = _find_num_pr(paragraph._p)
    if num_id is None:
        style = getattr(paragraph, "style", None)
        if style is not None:
            num_id, ilvl = _find_num_pr(getattr(style, "element", None))
    if num_id is None or num_id == 0:  # numId 0 means "no list"
        return None
    return num_id, ilvl or 0


def _find_num_pr(elem) -> tuple[int | None, int]:
    """Extract ``(numId, ilvl)`` from a ``<w:p>`` or ``<w:style>`` element."""
    if elem is None:
        return None, 0
    pPr = elem.find(qn("pPr"))
    if pPr is None:
        return None, 0
    numPr = pPr.find(qn("numPr"))
    if numPr is None:
        return None, 0
    num_id_el = numPr.find(qn("numId"))
    if num_id_el is None:
        return None, 0
    num_id = _to_int(num_id_el.get(qn("val")))
    ilvl_el = numPr.find(qn("ilvl"))
    ilvl = _to_int(ilvl_el.get(qn("val"))) if ilvl_el is not None else 0
    return num_id, ilvl or 0


# --------------------------------------------------------------------------- #
# Footnotes
# --------------------------------------------------------------------------- #


def _load_footnotes(path: Path) -> dict[int, str]:
    """Map footnote id → raw text from ``word/footnotes.xml`` in the .docx zip.

    python-docx does not expose the footnotes relationship (it only loads part
    types it knows about), so we read the zip directly. Returns ``{}`` when the
    document has no footnotes part.
    """
    footnotes: dict[int, str] = {}
    try:
        import zipfile

        with zipfile.ZipFile(str(path)) as zf:
            names = set(zf.namelist())
            if "word/footnotes.xml" not in names:
                return footnotes
            blob = zf.read("word/footnotes.xml")
        root = ET.fromstring(blob)
        for fn in root.findall(qn("footnote")):
            fid = _to_int(fn.get(qn("id")))
            if fid is None or fid in (-1, 0):  # -1/0 are separator/continuation
                continue
            texts = [t.text or "" for t in fn.iter(qn("t"))]
            footnotes[fid] = "".join(texts).strip()
    except Exception:
        return footnotes
    return footnotes


def _paragraph_footnote_refs(paragraph) -> list[int]:
    ids: list[int] = []
    for ref in paragraph._p.iter(qn("footnoteReference")):
        fid = _to_int(ref.get(qn("id")))
        if fid is not None:
            ids.append(fid)
    return ids


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class _RawCell:
    """Intermediate table cell parsed from ``<w:tc>`` before span resolution."""

    __slots__ = ("col", "col_span", "vmerge", "text", "row_span", "covered")

    def __init__(self, *, col: int, col_span: int, vmerge: str | None, text: str) -> None:
        self.col = col
        self.col_span = col_span
        self.vmerge = vmerge
        self.text = text
        self.row_span = 1
        self.covered = False


class DocxParser(BaseParser):
    file_type = "docx"

    def parse(self, path: str | Path, *, doc_id: str | None = None) -> Document:
        path = Path(path)
        import docx as python_docx

        doc = python_docx.Document(str(path))
        numbering = NumberingResolver.from_xml(self._read_numbering(doc))
        footnotes = _load_footnotes(path)

        body = doc.element.body
        # Iterate block-level children in document order. python-docx exposes
        # paragraphs and tables separately, losing order, so we walk the XML.
        elements: list[Element] = []
        section_path: list[tuple[int, str]] = []
        para_index = -1
        tbl_index = 0
        ordinal = 0
        p_count = 0
        t_count = 0

        for child in body.iterchildren():
            tag = child.tag
            if tag == qn("p"):
                para_index += 1
                p_count += 1
                # Find the python-docx Paragraph object wrapping this element.
                paragraph = self._wrap_paragraph(doc, child)
                if paragraph is None:
                    continue
                el = self._paragraph_to_element(
                    paragraph, path, para_index, numbering, ordinal,
                )
                if el is None:
                    continue
                ordinal += 1
                # Maintain section path from headings.
                if el.kind == ElementKind.HEADING and 0 < el.level <= 6:
                    while section_path and section_path[-1][0] >= el.level:
                        section_path.pop()
                    section_path.append((el.level, el.text.strip()))
                el.section = [t for _, t in section_path]
                elements.append(el)

                # Footnotes referenced in this paragraph, in order.
                for fid in _paragraph_footnote_refs(paragraph):
                    text = footnotes.get(fid, "")
                    if not text:
                        continue
                    ordinal += 1
                    elements.append(
                        Element(
                            id=self._id(path, f"p{para_index}", "footnote", str(fid)),
                            kind=ElementKind.FOOTNOTE,
                            source=SourceRef(
                                file=path.name, paragraph_index=para_index
                            ),
                            section=list(el.section),
                            text=self.normalize(text),
                            meta={"footnote_id": fid},
                        )
                    )
            elif tag == qn("tbl"):
                t_count += 1
                table = self._wrap_table(doc, child)
                if table is None:
                    continue
                el = self._table_to_element(table, path, tbl_index, para_index)
                tbl_index += 1
                if el is None:
                    continue
                ordinal += 1
                el.section = [t for _, t in section_path]
                elements.append(el)

        sections = self._build_sections(path, elements)
        return Document(
            id=doc_id or path.stem,
            source_file=path.name,
            file_type=self.file_type,
            elements=elements,
            sections=sections,
            metadata=self.make_metadata(
                path,
                {
                    "paragraph_count": p_count,
                    "table_count": t_count,
                    "footnote_count": sum(
                        1 for e in elements if e.kind == ElementKind.FOOTNOTE
                    ),
                },
            ),
        )

    # -- paragraph -------------------------------------------------------- #
    def _paragraph_to_element(
        self, paragraph, path: Path, para_index: int,
        numbering: NumberingResolver, ordinal: int,
    ) -> Element | None:
        text = self.normalize(paragraph.text or "")
        if not text:
            return None

        level = _heading_level(paragraph)
        num_pr = _num_pr(paragraph)

        source = SourceRef(file=path.name, paragraph_index=para_index)

        if level is not None:
            heading_num = self._extract_heading_number(text)
            kind = ElementKind.HEADING
            return Element(
                id=self._id(path, "p", str(para_index), "heading"),
                kind=kind,
                source=source,
                text=text,
                level=level,
                heading_number=heading_num,
                meta={"style": getattr(paragraph.style, "name", "")},
            )

        if num_pr is not None:
            num_id, ilvl = num_pr
            marker, ordered = numbering.marker(num_id, ilvl, self.normalizer)
            return Element(
                id=self._id(path, "p", str(para_index), "list", str(ordinal)),
                kind=ElementKind.LIST_ITEM,
                source=source,
                text=text,
                level=ilvl,
                list_marker=marker,
                list_index=0,  # filled by chunker/pipeline if needed
                list_id=f"num{num_id}",
                ordered=ordered,
                meta={"num_id": num_id, "ilvl": ilvl},
            )

        return Element(
            id=self._id(path, "p", str(para_index), "para"),
            kind=ElementKind.PARAGRAPH,
            source=source,
            text=text,
            meta={"style": getattr(paragraph.style, "name", "")},
        )

    @staticmethod
    def _extract_heading_number(text: str) -> str:
        m = re.match(r"^([0-9\u0660-\u0669\u06F0-\u06F9]+(?:\.[0-9\u0660-\u0669\u06F0-\u06F9]+)*)[\.،\)\s]", text)
        if m:
            return m.group(1)
        return ""

    # -- table ------------------------------------------------------------ #
    def _table_to_element(
        self, table, path: Path, tbl_index: int, para_index: int,
    ) -> Element | None:
        rows = self._build_table_rows(table)
        if not rows:
            return None
        return Element(
            id=self._id(path, "tbl", str(tbl_index)),
            kind=ElementKind.TABLE,
            source=SourceRef(
                file=path.name, paragraph_index=para_index
            ),
            rows=rows,
            meta={"row_count": len(rows), "col_count": max(len(r.cells) for r in rows)},
        )

    def _build_table_rows(self, table) -> list[TableRow]:
        """Build HTML-style rows with origin cells carrying spans.

        Parses the ``<w:tbl>`` XML directly (walking ``<w:tr>``/``<w:tc>`` and
        reading ``gridSpan``/``vMerge``) rather than trusting python-docx's
        ``cell(r, c)``, whose merge bookkeeping is unreliable after ``merge()``.
        Origin cells keep their ``row_span``/``col_span``; vMerge continuation
        cells are dropped (HTML-style), so a rowspan is expressed once.
        """
        tbl = table._tbl
        ncols = self._grid_col_count(tbl)
        if ncols == 0:
            return []

        trs = tbl.findall(qn("tr"))
        if not trs:
            return []

        # First pass: per row, list of raw cells with their grid column.
        raw_rows: list[list[_RawCell]] = []
        for r_idx, tr in enumerate(trs):
            cells: list[_RawCell] = []
            col = 0
            for tc in tr.findall(qn("tc")):
                gridspan, vmerge = self._tc_spans(tc)
                text = self.normalize(self._tc_text(tc))
                cells.append(_RawCell(col=col, col_span=gridspan, vmerge=vmerge, text=text))
                col += gridspan
            raw_rows.append(cells)

        # Second pass: compute row_span for vMerge "restart" cells by counting
        # consecutive "continue" cells below them at the same start column.
        for r_idx, cells in enumerate(raw_rows):
            for rc in cells:
                if rc.vmerge == "restart":
                    span = 1
                    for below in range(r_idx + 1, len(raw_rows)):
                        cont = next(
                            (bc for bc in raw_rows[below]
                             if bc.col == rc.col and bc.vmerge == "continue"),
                            None,
                        )
                        if cont is not None:
                            span += 1
                            cont.covered = True
                        else:
                            break
                    rc.row_span = span
                elif rc.vmerge == "continue":
                    rc.covered = True

        # Third pass: emit origin (non-covered) cells per row, HTML-style.
        rows_out: list[TableRow] = []
        first_row_is_header = self._looks_like_header(raw_rows[0] if raw_rows else [])
        for r_idx, cells in enumerate(raw_rows):
            out_cells: list[TableCell] = []
            for rc in cells:
                if rc.covered:
                    continue
                out_cells.append(
                    TableCell(
                        text=rc.text,
                        row_span=rc.row_span,
                        col_span=rc.col_span,
                        vmerge="restart" if rc.row_span > 1 else None,
                    )
                )
            if out_cells:
                rows_out.append(
                    TableRow(
                        index=r_idx,
                        cells=out_cells,
                        is_header=(r_idx == 0 and first_row_is_header),
                    )
                )
        return rows_out

    @staticmethod
    def _tc_spans(tc) -> tuple[int, str | None]:
        """Return ``(gridSpan, vMerge)`` for a ``<w:tc>`` element."""
        gridspan = 1
        vmerge: str | None = None
        tcPr = tc.find(qn("tcPr"))
        if tcPr is not None:
            gs = tcPr.find(qn("gridSpan"))
            if gs is not None:
                gridspan = _to_int(gs.get(qn("val"))) or 1
            vm = tcPr.find(qn("vMerge"))
            if vm is not None:
                val = vm.get(qn("val"))
                vmerge = "restart" if val == "restart" else "continue"
        return gridspan, vmerge

    @staticmethod
    def _tc_text(tc) -> str:
        """Concatenate all ``<w:t>`` text inside a ``<w:tc>``."""
        parts = [t.text or "" for t in tc.iter(qn("t"))]
        return "".join(parts).strip()

    @staticmethod
    def _grid_col_count(tbl) -> int:
        """Column count from ``<w:tblGrid>``; falls back to max row span."""
        grid = tbl.find(qn("tblGrid"))
        if grid is not None:
            cols = grid.findall(qn("gridCol"))
            if cols:
                return len(cols)
        # Fallback: max summed gridSpan across rows.
        trs = tbl.findall(qn("tr"))
        best = 0
        for tr in trs:
            total = 0
            for tc in tr.findall(qn("tc")):
                gs, _ = DocxParser._tc_spans(tc)
                total += gs
            best = max(best, total)
        return best

    @staticmethod
    def _looks_like_header(first_row: list["_RawCell"]) -> bool:
        """Heuristic: a short, fully non-empty first row is a header."""
        if not first_row:
            return False
        texts = [c.text.strip() for c in first_row if c.text.strip()]
        if not texts:
            return False
        return all(len(t) <= 40 for t in texts) and len(texts) >= len(first_row) - 1

    # -- helpers ---------------------------------------------------------- #
    def _id(self, path: Path, *parts) -> str:
        from ..ids import make_id
        return make_id(path.name, *parts)

    @staticmethod
    def _read_numbering(doc) -> bytes | None:
        try:
            for rel in doc.part.rels.values():
                if "numbering" in rel.reltype:
                    return rel.target_part.blob
        except Exception:
            return None
        return None

    @staticmethod
    def _wrap_paragraph(doc, p_elem):
        from docx.text.paragraph import Paragraph
        try:
            return Paragraph(p_elem, doc.part)
        except Exception:
            return None

    @staticmethod
    def _wrap_table(doc, tbl_elem):
        from docx.table import Table
        try:
            return Table(tbl_elem, doc.part)
        except Exception:
            return None

    def _build_sections(self, path: Path, elements: Iterable[Element]) -> list:
        from ..ir import Section
        root: list[Section] = []
        stack: list[Section] = []
        from ..ids import make_id
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
            if stack:
                stack[-1].children.append(sec)
            else:
                root.append(sec)
            stack.append(sec)
        return root
