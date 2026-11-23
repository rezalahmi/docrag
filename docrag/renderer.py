"""HTML renderer for visual quality review.

Turns a :class:`~docrag.ir.Document` into a single self-contained, responsive,
RTL HTML page so a human can eyeball whether the parser produced sensible
sections, tables and lists before anything is embedded.

The renderer is *derived* from the IR (it never re-reads the source file), so
it doubles as a round-trip check: if the HTML looks wrong, the IR is wrong, and
by extension every downstream chunk is wrong too.

Table rendering reconstructs the full grid from the origin-cell IR (which stores
only ``row_span``/``col_span``, not column positions) so ``rowspan``/``colspan``
land in the right columns — a non-trivial detail that naive renderers get wrong.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .ir import Document, Element, ElementKind, TableCell, TableRow


class HtmlRenderer:
    """Render a :class:`Document` to a self-contained HTML string."""

    def render(self, doc: Document) -> str:
        parts: list[str] = [
            "<!DOCTYPE html>",
            '<html lang="fa" dir="rtl">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{html.escape(doc.source_file)} — IR preview</title>",
            "<style>",
            _CSS,
            "</style>",
            "</head>",
            "<body>",
            self._header(doc),
            self._toc(doc),
            '<main class="doc">',
        ]
        for el in doc.elements:
            parts.append(self._render_element(el))
        parts.append("</main>")
        parts.append("</body></html>")
        return "\n".join(parts)

    # -- header / toc ----------------------------------------------------- #
    def _header(self, doc: Document) -> str:
        meta = doc.metadata or {}
        stats = self._stats(doc)
        rows = [
            ("فایل", doc.source_file),
            ("نوع", doc.file_type),
            ("عناصر", str(len(doc.elements))),
            ("بخش‌ها", str(len(doc.sections))),
            ("صفحه‌ها / پاراگراف‌ها", str(meta.get("page_count") or meta.get("paragraph_count") or "—")),
            ("جداول", str(stats["tables"])),
            ("فهرست‌ها", str(stats["list_items"])),
            ("تبصره‌ها", str(stats["footnotes"])),
        ]
        body = "\n".join(
            f'<div class="meta-row"><span class="meta-k">{html.escape(k)}</span>'
            f'<span class="meta-v">{html.escape(v)}</span></div>'
            for k, v in rows
        )
        return f'<header class="doc-header"><h1>{html.escape(doc.source_file)}</h1><div class="meta">{body}</div></header>'

    def _toc(self, doc: Document) -> str:
        if not doc.sections:
            return ""
        items: list[str] = []
        for sec in doc.sections:
            items.append(self._toc_item(sec))
        return f'<nav class="toc"><h2>فهرست بخش‌ها</h2><ul>{"".join(items)}</ul></nav>'

    def _toc_item(self, sec) -> str:
        label = f'{sec.number + " " if sec.number else ""}{sec.title}'
        children = "".join(self._toc_item(c) for c in sec.children) if sec.children else ""
        sub = f"<ul>{children}</ul>" if children else ""
        return f'<li class="lvl-{sec.level}"><a href="#{html.escape(sec.id)}">{html.escape(label)}</a>{sub}</li>'

    # -- element ---------------------------------------------------------- #
    def _render_element(self, el: Element) -> str:
        kind = el.kind
        if kind == ElementKind.HEADING:
            return self._heading(el)
        if kind == ElementKind.PARAGRAPH:
            return self._paragraph(el)
        if kind == ElementKind.LIST_ITEM:
            return self._list_item(el)
        if kind == ElementKind.TABLE:
            return self._table(el)
        if kind == ElementKind.FOOTNOTE:
            return self._footnote(el)
        return self._generic(el)

    def _heading(self, el: Element) -> str:
        level = max(1, min(6, el.level or 1))
        num = f'<span class="hnum">{html.escape(el.heading_number)}</span> ' if el.heading_number else ""
        anchor = f'<a id="{html.escape(el.id)}" class="anchor"></a>'
        badge = self._source_badge(el)
        return f'<section class="block heading">{anchor}<h{level}>{num}{html.escape(el.text)}</h{level}>{badge}</section>'

    def _paragraph(self, el: Element) -> str:
        anchor = f'<a id="{html.escape(el.id)}" class="anchor"></a>'
        badge = self._source_badge(el)
        return f'<section class="block para">{anchor}<p>{html.escape(el.text)}</p>{badge}</section>'

    def _list_item(self, el: Element) -> str:
        anchor = f'<a id="{html.escape(el.id)}" class="anchor"></a>'
        marker = f'<span class="marker">{html.escape(el.list_marker)}</span>' if el.list_marker else ""
        indent = (el.level or 0) * 1.5
        badge = self._source_badge(el)
        return (
            f'<section class="block list-item" style="margin-right:{indent}rem">'
            f'{anchor}<div class="li">{marker}<span>{html.escape(el.text)}</span></div>{badge}</section>'
        )

    def _footnote(self, el: Element) -> str:
        anchor = f'<a id="{html.escape(el.id)}" class="anchor"></a>'
        badge = self._source_badge(el)
        return f'<section class="block footnote">{anchor}<p><sup>*</sup> {html.escape(el.text)}</p>{badge}</section>'

    def _generic(self, el: Element) -> str:
        anchor = f'<a id="{html.escape(el.id)}" class="anchor"></a>'
        badge = self._source_badge(el)
        return f'<section class="block generic">{anchor}<p>{html.escape(el.text)}</p>{badge}</section>'

    def _table(self, el: Element) -> str:
        anchor = f'<a id="{html.escape(el.id)}" class="anchor"></a>'
        badge = self._source_badge(el)
        if not el.rows:
            return f'<section class="block table-empty">{anchor}<p>(جدول خالی)</p>{badge}</section>'
        html_table = self._html_table(el.rows)
        caption = f'<figcaption>{html.escape(el.caption)}</figcaption>' if el.caption else ""
        return f'<section class="block table">{anchor}<figure>{caption}{html_table}</figure>{badge}</section>'

    def _html_table(self, rows: list[TableRow]) -> str:
        """Render an HTML table honouring row_span / col_span / vmerge.

        Column positions are recomputed from the origin-cell IR by simulating
        the grid: for each row, find the first free column, place the cell, and
        mark the spanned rectangle occupied. This makes ``rowspan`` from a cell
        above correctly push lower-row cells to the right column.
        """
        ncols = max((sum(c.col_span for c in r.cells) for r in rows), default=0)
        if ncols == 0:
            return "<table></table>"
        occupied: list[list[bool]] = [[False] * ncols for _ in range(len(rows))]
        out: list[str] = ['<table class="data">']
        for r_idx, row in enumerate(rows):
            out.append("<tr>")
            # Walk free columns left to right; emit a cell for the next origin
            # cell, or an empty <td> if the column is genuinely empty.
            cell_iter = iter(row.cells)
            placed_cells = 0
            c = 0
            while c < ncols:
                if occupied[r_idx][c]:
                    c += 1
                    continue
                try:
                    cell = next(cell_iter)
                except StopIteration:
                    # Sparse row: emit empty cell for the remaining free column.
                    out.append("<td></td>")
                    c += 1
                    continue
                rs = max(1, cell.row_span)
                cs = max(1, cell.col_span)
                # Clamp to remaining grid.
                rs = min(rs, len(rows) - r_idx)
                cs = min(cs, ncols - c)
                for rr in range(r_idx, r_idx + rs):
                    for cc in range(c, c + cs):
                        if rr < len(rows) and cc < ncols:
                            occupied[rr][cc] = True
                attrs = ""
                if rs > 1:
                    attrs += f' rowspan="{rs}"'
                if cs > 1:
                    attrs += f' colspan="{cs}"'
                cls = "th" if row.is_header else "td"
                out.append(f'<{cls}{attrs}>{html.escape(cell.text)}</{cls}>')
                placed_cells += 1
                c += cs
            out.append("</tr>")
        out.append("</table>")
        return "\n".join(out)

    # -- source badge ----------------------------------------------------- #
    def _source_badge(self, el: Element) -> str:
        src = el.source
        bits: list[str] = []
        if src.page is not None:
            bits.append(f"صفحه {src.page}")
        if src.paragraph_index is not None:
            bits.append(f"پاراگراف {src.paragraph_index}")
        if src.bbox:
            bits.append(
                f"مختصات [{src.bbox[0]:.0f},{src.bbox[1]:.0f},{src.bbox[2]:.0f},{src.bbox[3]:.0f}]"
            )
        loc = " · ".join(bits)
        kind_label = {
            ElementKind.HEADING: "عنوان",
            ElementKind.PARAGRAPH: "پاراگراف",
            ElementKind.LIST_ITEM: "فهرست",
            ElementKind.TABLE: "جدول",
            ElementKind.FOOTNOTE: "تبصره",
        }.get(el.kind, el.kind.value)
        return (
            f'<div class="src-badge"><code>{html.escape(el.id)}</code>'
            f'<span class="kind">{kind_label}</span>'
            f'<span class="loc">{html.escape(loc)}</span></div>'
        )

    # -- stats ------------------------------------------------------------ #
    @staticmethod
    def _stats(doc: Document) -> dict[str, int]:
        counts = {"tables": 0, "list_items": 0, "footnotes": 0, "headings": 0, "paragraphs": 0}
        for el in doc.elements:
            if el.kind == ElementKind.TABLE:
                counts["tables"] += 1
            elif el.kind == ElementKind.LIST_ITEM:
                counts["list_items"] += 1
            elif el.kind == ElementKind.FOOTNOTE:
                counts["footnotes"] += 1
            elif el.kind == ElementKind.HEADING:
                counts["headings"] += 1
            elif el.kind == ElementKind.PARAGRAPH:
                counts["paragraphs"] += 1
        return counts


def render_html(doc: Document) -> str:
    """Render ``doc`` to a self-contained HTML string."""
    return HtmlRenderer().render(doc)


def write_html(doc: Document, path: str | Path) -> Path:
    """Render ``doc`` and write the HTML to ``path``."""
    path = Path(path)
    path.write_text(render_html(doc), encoding="utf-8")
    return path


_CSS = """
*{box-sizing:border-box}
body{font-family:"Vazirmatn","IRANSans","Segoe UI",Tahoma,sans-serif;
  margin:0;background:#f7f7f9;color:#1f2328;line-height:1.8;font-size:15px}
.doc-header{background:#1f6feb;color:#fff;padding:18px 20px}
.doc-header h1{margin:0 0 12px;font-size:clamp(1.1rem,3vw,1.5rem);font-weight:700}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px}
.meta-row{display:inline-flex;gap:6px;font-size:13px}
.meta-k{opacity:.8}
.meta-v{font-weight:600}
.toc{background:#fff;border-bottom:1px solid #e2e2e6;padding:12px 20px;font-size:14px}
.toc h2{margin:0 0 6px;font-size:14px;color:#6a737d}
.toc ul{list-style:none;padding-right:0;margin:0}
.toc li{padding:1px 0}
.toc .lvl-2{padding-right:16px}.toc .lvl-3{padding-right:32px}
.toc a{color:#0969da;text-decoration:none}
.toc a:hover{text-decoration:underline}
.doc{max-width:920px;margin:18px auto;padding:0 16px}
.block{background:#fff;border:1px solid #e7e7ea;border-radius:8px;
  padding:12px 16px;margin-bottom:12px;position:relative}
.block.heading{background:#eef4ff;border-color:#cfe0ff}
.block.heading h1,.block.heading h2,.block.heading h3,
.block.heading h4,.block.heading h5,.block.heading h6{margin:.1em 0;color:#0b3d91}
.hnum{color:#6a737d;font-weight:600;font-size:.9em}
.block.para p{margin:.2em 0}
.block.list-item{padding:8px 16px}
.block.list-item .li{display:flex;gap:8px;align-items:baseline}
.marker{color:#0969da;font-weight:700;min-width:1.5em;display:inline-block}
.block.footnote{background:#fffdf0;border-color:#f0e6b3;font-size:13px}
.block.footnote sup{color:#b08800}
.block.table figure{margin:0}
.block.table figcaption{font-size:13px;color:#6a737d;margin-bottom:6px}
table.data{border-collapse:collapse;width:100%;font-size:13px}
table.data th,table.data td{border:1px solid #d0d4da;padding:6px 8px;text-align:right;vertical-align:top}
table.data th{background:#f0f4ff;color:#0b3d91;font-weight:600}
.anchor{display:block;position:relative;top:-6px;visibility:hidden}
.src-badge{margin-top:8px;font-size:11px;color:#6a737d;display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-top:1px dashed #ececf0;padding-top:6px}
.src-badge code{background:#f3f3f5;padding:1px 6px;border-radius:4px;color:#57606a}
.src-badge .kind{background:#eef4ff;color:#0b3d91;padding:1px 8px;border-radius:10px;font-weight:600}
.src-badge .loc{font-variant-numeric:tabular-nums}
@media (max-width:640px){
  .doc{padding:0 8px}
  .block{padding:10px 12px}
  table.data{font-size:12px}
  table.data th,table.data td{padding:4px 6px}
}
"""
