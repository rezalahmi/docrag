"""XLSX parser (openpyxl, MIT).

Turns a workbook into the same :class:`~docrag.ir.Document` IR as PDF/DOCX, so
the downstream chunker, renderer and validator are reused unchanged.

Excel raises four problems a generic text parser cannot solve, and this parser
solves each:

1. **Table detection.** A sheet is split into independent table regions by
   blank rows and blank columns (merged-aware, so a merged title or parent
   header is not mistaken for a gap).
2. **Title + multi-level headers.** A merged cell spanning the table width
   above the header is the table title. Parent headers merged across columns
   are combined with their child headers, so column F becomes ``"سود"`` and
   column B becomes ``"درآمد کل"``.
3. **Merged cells in data.** A merged value spanning several rows (e.g. a
   ``منطقه`` column covering two agency rows) is *propagated* to each spanned
   row, so every chunk is self-contained; the anchor cell keeps its value and
   the propagated cell's ``address`` points back to the anchor.
4. **Formula values.** The workbook is opened twice (normal + ``data_only``).
   A formula with a cached result yields that result; a formula without one is
   flagged ``needs_review`` and the formula string never reaches a chunk.

Each data row is emitted as its own ``TABLE`` element (header row + that one
data row). That keeps row-level retrieval exact: the chunker produces one
self-contained chunk per row with ``colname: value`` pairs and a per-cell
``cell_refs`` map (header → address) for citation.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import coordinate_to_tuple, get_column_letter

from ..ids import make_id
from ..ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from ..normalizer import Normalizer, NormalizerConfig
from .base import BaseParser

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_LATIN_TO_PERSIAN = str.maketrans("0123456789", _PERSIAN_DIGITS)


def format_number(value, number_format: str) -> str:
    """Render a numeric value to a Persian display string honouring grouping.

    openpyxl gives the raw number and the format code (e.g. ``#,##0``) but does
    not apply the format. Grouping is applied when the format asks for it, then
    digits → Persian and the thousands comma → the Persian separator ``٬``.
    """
    if value is None:
        return ""
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return str(value)
    grouped = bool(re.search(r"#[,0][,0#]*|0,0", number_format or ""))
    is_int = fval == int(fval) and "0." not in (number_format or "")
    if grouped:
        s = f"{int(fval):,}" if is_int else f"{fval:,f}"
    else:
        s = str(int(fval)) if is_int else str(fval)
    s = s.translate(_LATIN_TO_PERSIAN)
    s = s.replace(",", "٬")
    return s


@dataclass
class _XCell:
    address: str
    row: int
    col: int
    text: str
    needs_review: bool = False
    is_continuation: bool = False
    anchor: str | None = None


class XlsxParser(BaseParser):
    """Parse an .xlsx workbook into a :class:`Document`."""

    file_type = "xlsx"

    def __init__(self, normalizer: Normalizer | None = None) -> None:
        # Excel needs identifiers (phone, code) to keep their Latin digits AND
        # exact cell values to keep their display form (آ must not fold to ا),
        # so string cells are normalised with digits="keep" and letter
        # canonicalisation OFF. A separate normaliser instance is kept so the
        # pipeline's shared normaliser (digits="persian", canonicalise ON)
        # cannot override this when it is assigned to self.normalizer. Search
        # matching still works because the retriever folds both sides at query
        # time.
        super().__init__(normalizer)
        self._str_norm = Normalizer(NormalizerConfig(
            digits="keep", canonicalize_letters=False,
        ))

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def parse(self, path: str | Path, *, doc_id: str | None = None) -> Document:
        path = Path(path)
        wb = openpyxl.load_workbook(path, data_only=False)
        wb_data = openpyxl.load_workbook(path, data_only=True)
        try:
            elements: list[Element] = []
            para = 0
            for idx, name in enumerate(wb.sheetnames, start=1):
                ws = wb[name]
                ws_data = wb_data[name]
                if ws.sheet_state == "veryHidden":
                    continue
                merged_index = self._build_merged_index(ws)
                tables = self._detect_tables(ws, ws_data, merged_index)
                hidden_rows = [r for r, dim in ws.row_dimensions.items() if dim.hidden]
                # Sheet heading (level 1) — populates the section tree.
                para += 1
                elements.append(self._heading(name, level=1, doc_id=path.stem,
                                              src_file=path.name, para=para,
                                              section=[name]))
                for tbl in tables:
                    if tbl["title"] and tbl["title"] != name:
                        para += 1
                        elements.append(self._heading(
                            tbl["title"], level=2, doc_id=path.stem,
                            src_file=path.name, para=para, section=[name, tbl["title"]]))
                    for row_cells in tbl["data_rows"]:
                        para += 1
                        elements.append(self._row_element(
                            path=path, sheet=name, table=tbl,
                            row_cells=row_cells, para=para,
                            hidden=(row_cells[0].row in hidden_rows),
                        ))
            doc = Document(
                id=doc_id or path.stem,
                source_file=path.name,
                file_type=self.file_type,
                elements=elements,
                sections=self._section_tree(elements),
                metadata=self.make_metadata(path, {
                    "sheet_count": len(wb.sheetnames),
                    "sheet_names": list(wb.sheetnames),
                    "table_count": sum(1 for e in elements if e.kind == ElementKind.TABLE),
                    "hidden_sheets": [n for n in wb.sheetnames if wb[n].sheet_state != "visible"],
                    "hidden_rows": {
                        n: [r for r, dim in wb[n].row_dimensions.items() if dim.hidden]
                        for n in wb.sheetnames
                    },
                }),
            )
            return doc
        finally:
            wb.close()
            wb_data.close()

    # ------------------------------------------------------------------ #
    # element builders
    # ------------------------------------------------------------------ #
    def _heading(self, text: str, *, level: int, doc_id: str, src_file: str,
                 para: int, section: list[str]) -> Element:
        return Element(
            id=make_id(doc_id, "h", para),
            kind=ElementKind.HEADING,
            source=SourceRef(file=src_file, paragraph_index=para - 1),
            section=list(section),
            text=self._str_norm.normalize(text),
            level=level,
        )

    def _row_element(self, *, path, sheet, table, row_cells, para, hidden) -> Element:
        headers = table["combined_headers"]
        ncols = len(headers)
        header_row = TableRow(
            index=0, is_header=True,
            cells=[TableCell(text=h, address="") for h in headers],
        )
        data_cells: list[TableCell] = []
        needs_review: list[str] = []
        for ci, xc in enumerate(row_cells[:ncols]):
            header = headers[ci] if ci < len(headers) else f"ستون {get_column_letter(xc.col)}"
            addr = xc.anchor if (xc.is_continuation and xc.anchor) else xc.address
            if xc.needs_review:
                needs_review.append(header)
            data_cells.append(TableCell(text=xc.text, address=addr))
        data_row = TableRow(index=1, is_header=False, cells=data_cells)
        first = row_cells[0]
        last = row_cells[-1]
        row_range = f"{get_column_letter(first.col)}{first.row}:{get_column_letter(last.col)}{last.row}"
        return Element(
            id=make_id(path.stem, sheet, "row", first.row),
            kind=ElementKind.TABLE,
            source=SourceRef(file=path.name, paragraph_index=para - 1),
            section=[sheet, table["title"] or sheet],
            rows=[header_row, data_row],
            caption=table["title"],
            meta={
                "sheet": sheet,
                "table": table["title"] or sheet,
                "row": first.row,
                "range": row_range,
                "hidden": hidden,
                "needs_review": needs_review,
                "combined_headers": list(headers),
            },
        )

    @staticmethod
    def _section_tree(elements: list[Element]) -> list:
        from ..ir import Section

        roots: list[Section] = []
        stack: list[tuple[int, Section]] = []
        for el in elements:
            if el.kind != ElementKind.HEADING or el.level <= 0:
                continue
            sec = Section(id=el.id, title=el.text, level=el.level)
            while stack and stack[-1][0] >= el.level:
                stack.pop()
            if stack:
                stack[-1][1].children.append(sec)
            else:
                roots.append(sec)
            stack.append((el.level, sec))
        return roots

    # ------------------------------------------------------------------ #
    # merged-cell index
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_merged_index(ws) -> tuple[dict, dict]:
        cell_to_anchor: dict[tuple[int, int], str] = {}
        anchor_range: dict[str, tuple[int, int, int, int]] = {}
        for mr in ws.merged_cells.ranges:
            anchor = f"{get_column_letter(mr.min_col)}{mr.min_row}"
            anchor_range[anchor] = (mr.min_row, mr.min_col, mr.max_row, mr.max_col)
            for r in range(mr.min_row, mr.max_row + 1):
                for c in range(mr.min_col, mr.max_col + 1):
                    cell_to_anchor[(r, c)] = anchor
        return cell_to_anchor, anchor_range

    def _raw_at(self, ws, ws_data, r, c, merged_index):
        cell_to_anchor, _ = merged_index
        anchor = cell_to_anchor.get((r, c))
        if anchor is not None:
            ar, ac = coordinate_to_tuple(anchor)
            if (ar, ac) == (r, c):
                return (ws.cell(ar, ac).value,
                        ws_data.cell(ar, ac).value if ws_data is not None else None,
                        None, False)
            return (ws.cell(ar, ac).value,
                    ws_data.cell(ar, ac).value if ws_data is not None else None,
                    anchor, True)
        return (ws.cell(r, c).value,
                ws_data.cell(r, c).value if ws_data is not None else None,
                None, False)

    def _filled(self, ws, r, c, merged_index) -> bool:
        raw, _, _, _ = self._raw_at(ws, None, r, c, merged_index)
        return raw is not None and not (isinstance(raw, str) and not raw.strip())

    # ------------------------------------------------------------------ #
    # table detection
    # ------------------------------------------------------------------ #
    def _used_bounds(self, ws, merged_index):
        max_r = 0
        max_c = 0
        for row in ws.iter_rows():
            for cl in row:
                if cl.value is not None and not (isinstance(cl.value, str) and not cl.value.strip()):
                    if cl.row > max_r:
                        max_r = cl.row
                    if cl.column > max_c:
                        max_c = cl.column
        if not max_r or not max_c:
            return None
        return (1, 1, max_r, max_c)

    def _detect_tables(self, ws, ws_data, merged_index) -> list[dict]:
        bounds = self._used_bounds(ws, merged_index)
        if bounds is None:
            return []
        _, _, max_r, max_c = bounds
        regions = self._find_regions(ws, merged_index, max_r, max_c)
        tables: list[dict] = []
        for reg in regions:
            tbl = self._build_table(ws, ws_data, merged_index, reg)
            if tbl is not None:
                tables.append(tbl)
        return tables

    def _find_regions(self, ws, merged_index, max_r, max_c):
        row_blocks = []
        cur = None
        for r in range(1, max_r + 1):
            filled = any(self._filled(ws, r, c, merged_index) for c in range(1, max_c + 1))
            if filled:
                cur = [r, r] if cur is None else [cur[0], r]
            elif cur is not None:
                row_blocks.append(cur)
                cur = None
        if cur is not None:
            row_blocks.append(cur)
        regions = []
        for rb in row_blocks:
            col_blocks = []
            cc = None
            for c in range(1, max_c + 1):
                filled = any(self._filled(ws, r, c, merged_index) for r in range(rb[0], rb[1] + 1))
                if filled:
                    cc = [c, c] if cc is None else [cc[0], c]
                elif cc is not None:
                    col_blocks.append(cc)
                    cc = None
            if cc is not None:
                col_blocks.append(cc)
            for cb in col_blocks:
                regions.append((rb[0], cb[0], rb[1], cb[1]))
        return regions

    # ------------------------------------------------------------------ #
    # table structure
    # ------------------------------------------------------------------ #
    def _build_table(self, ws, ws_data, merged_index, region) -> dict | None:
        min_r, min_c, max_r, max_c = region
        nrows = max_r - min_r + 1
        ncols = max_c - min_c + 1
        if nrows < 1 or ncols < 1:
            return None

        title = ""
        header_start = min_r
        if nrows >= 2:
            spanning = self._spanning_merge(ws, min_r, min_c, max_c, merged_index)
            nonempty_cols = [c for c in range(min_c, max_c + 1)
                             if self._filled(ws, min_r, c, merged_index)]
            if spanning is not None or len(nonempty_cols) == 1:
                col = nonempty_cols[0] if nonempty_cols else min_c
                raw, cached, _, _ = self._raw_at(ws, ws_data, min_r, col, merged_index)
                title = self._cell_text(raw, cached, ws, min_r, col)
                header_start = min_r + 1

        header_row_nums: list[int] = []
        r = header_start
        while r <= max_r and (max_r - r) >= 1 and self._row_is_header(ws, r, min_c, max_c, merged_index):
            header_row_nums.append(r)
            r += 1
        if not header_row_nums and (max_r - header_start + 1) >= 2:
            header_row_nums = [header_start]
            header_start += 1
        data_start = header_start + len(header_row_nums)
        if data_start > max_r:
            return None

        header_grid = [
            [self._make_cell(ws, ws_data, rr, cc, merged_index) for cc in range(min_c, max_c + 1)]
            for rr in header_row_nums
        ]
        combined = self._combine_headers(header_grid)
        data_rows = [
            [self._make_cell(ws, ws_data, rr, cc, merged_index) for cc in range(min_c, max_c + 1)]
            for rr in range(data_start, max_r + 1)
        ]
        return {"title": title.strip(), "combined_headers": combined,
                "data_rows": data_rows, "header_rows": header_grid}

    def _spanning_merge(self, ws, r, min_c, max_c, merged_index):
        cell_to_anchor, _ = merged_index
        anchor = cell_to_anchor.get((r, min_c))
        if anchor is None:
            return None
        for c in range(min_c, max_c + 1):
            if cell_to_anchor.get((r, c)) != anchor:
                return None
        return anchor

    def _row_is_header(self, ws, r, min_c, max_c, merged_index) -> bool:
        cells = []
        for c in range(min_c, max_c + 1):
            raw, _, _, _ = self._raw_at(ws, None, r, c, merged_index)
            if raw is not None and not (isinstance(raw, str) and not raw.strip()):
                anchor = merged_index[0].get((r, c))
                if anchor is not None:
                    ar, ac = coordinate_to_tuple(anchor)
                    cells.append((ws.cell(ar, ac), raw))
                else:
                    cells.append((ws.cell(r, c), raw))
        if not cells:
            return False
        nonempty = len(cells)
        bold = sum(1 for cl, _ in cells if cl.font and cl.font.bold)
        filled = sum(1 for cl, _ in cells if cl.fill and cl.fill.patternType is not None)
        if bold >= max(1, nonempty // 2) or filled >= max(1, nonempty // 2):
            return True
        all_text = all(isinstance(v, str) for _, v in cells)
        next_has_num = False
        for c in range(min_c, max_c + 1):
            raw, _, _, _ = self._raw_at(ws, None, r + 1, c, merged_index)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                next_has_num = True
                break
        return all_text and next_has_num

    # ------------------------------------------------------------------ #
    # cell construction (formula + merged + format)
    # ------------------------------------------------------------------ #
    def _make_cell(self, ws, ws_data, r, c, merged_index) -> _XCell:
        raw, cached, anchor, is_cont = self._raw_at(ws, ws_data, r, c, merged_index)
        address = f"{get_column_letter(c)}{r}"
        if is_cont:
            ar, ac = coordinate_to_tuple(anchor)
            src_cell = ws.cell(ar, ac)
            src_data = ws_data.cell(ar, ac)
        else:
            src_cell = ws.cell(r, c)
            src_data = ws_data.cell(r, c)
        number_format = src_cell.number_format or "General"
        is_formula = isinstance(raw, str) and raw.startswith("=")
        if is_formula:
            if cached is None or (isinstance(cached, str) and cached.startswith("=")):
                text = "نیازمند بررسی (فرمول بدون مقدار ذخیره‌شده)"
                needs_review = True
            else:
                text = self._format_value(cached, number_format)
                needs_review = False
        else:
            text = self._format_value(raw, number_format)
            needs_review = False
        return _XCell(address=address, row=r, col=c, text=text,
                      needs_review=needs_review,
                      is_continuation=is_cont, anchor=anchor)

    def _format_value(self, value, number_format: str) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "درست" if value else "نادرست"
        if isinstance(value, (int, float)):
            return format_number(value, number_format)
        if isinstance(value, _dt.datetime):
            return self._str_norm.normalize(value.strftime("%Y/%m/%d %H:%M"))
        if isinstance(value, _dt.date):
            return self._str_norm.normalize(value.strftime("%Y/%m/%d"))
        return self._str_norm.normalize(str(value))

    def _cell_text(self, raw, cached, ws, r, c) -> str:
        number_format = ws.cell(r, c).number_format or "General"
        is_formula = isinstance(raw, str) and raw.startswith("=")
        val = cached if (is_formula and cached is not None) else raw
        return self._format_value(val, number_format)

    # ------------------------------------------------------------------ #
    # multi-level header combination
    # ------------------------------------------------------------------ #
    def _combine_headers(self, header_grid: list[list[_XCell]]) -> list[str]:
        """Flatten one or more header rows into one header per data column.

        A merged continuation in a header cell is either a *rowspan* (anchor in
        the same column — the header continues down, so it is skipped) or a
        *colspan* (anchor in a different column — the parent header applies to
        this column, so it is kept). Consecutive duplicates are collapsed so a
        rowspan never produces ``"سال سال"``.
        """
        if not header_grid:
            return []
        ncols = len(header_grid[0])
        combined: list[str] = []
        for c in range(ncols):
            texts: list[str] = []
            for hr in header_grid:
                cell = hr[c]
                own_col = self._col_of(cell.address)
                anchor_col = self._anchor_col(cell)
                if cell.is_continuation and anchor_col == own_col:
                    continue  # vertical continuation (rowspan): same header
                t = cell.text.strip()
                if t:
                    texts.append(t)
            deduped: list[str] = []
            for t in texts:
                if not deduped or deduped[-1] != t:
                    deduped.append(t)
            combined.append(" ".join(deduped).strip())
        return combined

    @staticmethod
    def _col_of(address: str) -> int:
        """``"F4"`` → column index 6."""
        letters = "".join(ch for ch in address if ch.isalpha())
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
        return n

    def _anchor_col(self, cell: _XCell) -> int:
        if not cell.is_continuation or not cell.anchor:
            return self._col_of(cell.address)
        return self._col_of(cell.anchor)
