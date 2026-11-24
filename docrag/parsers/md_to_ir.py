"""Lightweight Markdown → Document IR converter.

Turns the Markdown produced by :mod:`docrag.parsers.html_to_md` (or any
``.md`` file) into :class:`~docrag.ir.Element` objects. This is deliberately a
small, dependency-free parser: it handles the structures that matter for
retrieval — ATX headings, paragraphs, ordered/unordered lists, pipe tables and
YAML front-matter — and ignores the long tail of Markdown syntax that rarely
carries retrieval signal in ingested web pages.

The output feeds straight into :class:`~docrag.chunker.SemanticChunker`, so a
URL ends up as the same kind of chunks as a PDF or DOCX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..ids import make_id, slugify
from ..ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from ..normalizer import Normalizer

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_OL_ITEM = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_UL_ITEM = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*(:?-{2,}:?\s*\|\s*)+:?-{2,}:?\s*\|?\s*$")
_FRONT_DELIM = re.compile(r"^---\s*$")
_FRONT_KV = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


@dataclass
class _MdParse:
    elements: list[Element]
    metadata: dict[str, Any]
    sections: list  # list[Section]


def markdown_to_document(
    md: str,
    *,
    doc_id: str,
    source_file: str,
    normalizer: Normalizer | None = None,
    file_type: str = "md",
) -> Document:
    """Parse ``md`` into a :class:`Document`."""
    norm = normalizer or Normalizer()
    parsed = _parse(md, doc_id=doc_id, source_file=source_file, normalizer=norm)
    return Document(
        id=doc_id,
        source_file=source_file,
        file_type=file_type,
        elements=parsed.elements,
        sections=parsed.sections,
        metadata=parsed.metadata,
    )


def _parse(md: str, *, doc_id: str, source_file: str, normalizer: Normalizer) -> _MdParse:
    lines = md.split("\n")
    elements: list[Element] = []
    sections: list = []
    metadata: dict[str, Any] = {"file_name": source_file, "file_type": "md"}
    ordinal = 0

    # --- front matter ---
    start = 0
    if lines and _FRONT_DELIM.match(lines[0]):
        end = None
        for i in range(1, len(lines)):
            if _FRONT_DELIM.match(lines[i]):
                end = i
                break
        if end is not None:
            for ln in lines[1:end]:
                m = _FRONT_KV.match(ln)
                if m:
                    val = m.group(2).strip().strip('"')
                    metadata[m.group(1)] = val
            start = end + 1

    section_path: list[tuple[int, str]] = []

    def _push_heading(level: int, text: str) -> None:
        while section_path and section_path[-1][0] >= level:
            section_path.pop()
        section_path.append((level, text))

    def _section_list() -> list[str]:
        return [t for _, t in section_path]

    def _src() -> SourceRef:
        nonlocal ordinal
        ordinal += 1
        return SourceRef(file=source_file, paragraph_index=ordinal - 1)

    # --- main loop ---
    i = start
    n = len(lines)
    para_buf: list[str] = []
    list_buf: list[tuple[str, str, bool]] = []  # (marker, text, ordered)
    list_id_counter = 0

    def _flush_para() -> None:
        nonlocal para_buf
        if not para_buf:
            return
        text = normalizer.normalize(" ".join(p.strip() for p in para_buf if p.strip()))
        para_buf = []
        if not text:
            return
        el = Element(
            id=make_id(doc_id, "p", ordinal),
            kind=ElementKind.PARAGRAPH,
            source=_src(),
            section=_section_list(),
            text=text,
        )
        elements.append(el)

    def _flush_list() -> None:
        nonlocal list_buf, list_id_counter
        if not list_buf:
            return
        list_id_counter += 1
        lid = f"{doc_id}::list::{list_id_counter}"
        ordered = list_buf[0][2]
        for idx, (marker, text, _) in enumerate(list_buf, start=1):
            el = Element(
                id=make_id(doc_id, "li", ordinal),
                kind=ElementKind.LIST_ITEM,
                source=_src(),
                section=_section_list(),
                text=normalizer.normalize(text),
                list_marker=marker,
                list_index=idx,
                list_id=lid,
                ordered=ordered,
            )
            elements.append(el)
        list_buf = []

    while i < n:
        line = lines[i]

        # Blank line: flush buffers.
        if not line.strip():
            _flush_para()
            _flush_list()
            i += 1
            continue

        # Heading.
        m = _ATX_HEADING.match(line)
        if m:
            _flush_para()
            _flush_list()
            level = len(m.group(1))
            text = normalizer.normalize(m.group(2).strip())
            _push_heading(level, text)
            elements.append(
                Element(
                    id=make_id(doc_id, "h", ordinal),
                    kind=ElementKind.HEADING,
                    source=_src(),
                    section=_section_list(),
                    text=text,
                    level=level,
                )
            )
            i += 1
            continue

        # Table: a header row followed by a separator row.
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            _flush_para()
            _flush_list()
            i = _consume_table(lines, i, doc_id=doc_id, source_file=source_file,
                               section=_section_list(), normalizer=normalizer,
                               elements=elements, src_fn=_src)
            continue

        # List item.
        mol = _OL_ITEM.match(line)
        mul = _UL_ITEM.match(line)
        if mol or mul:
            _flush_para()
            is_ordered = bool(mol)
            marker = (mol.group(2) + "." if mol else mul.group(2))
            text = (mol.group(3) if mol else mul.group(3))
            # New list group if the buffer is empty or ordering changed.
            if list_buf and list_buf[0][2] != is_ordered:
                _flush_list()
            list_buf.append((marker, text, is_ordered))
            i += 1
            continue

        # Blockquote line: treat as paragraph text (drop the leading ">").
        if line.lstrip().startswith(">"):
            _flush_list()
            para_buf.append(line.lstrip()[1:].strip())
            i += 1
            continue

        # Regular paragraph line.
        _flush_list()
        para_buf.append(line.strip())
        i += 1

    _flush_para()
    _flush_list()

    # Build the section tree from headings.
    sections = _build_section_tree(elements)
    return _MdParse(elements=elements, metadata=metadata, sections=sections)


def _consume_table(lines, i, *, doc_id, source_file, section, normalizer,
                   elements, src_fn) -> int:
    """Consume a pipe-table starting at line ``i``; append a TABLE element."""
    header_cells = _split_row(lines[i])
    i += 2  # skip header + separator
    rows: list[TableRow] = []
    # header row
    rows.append(TableRow(
        index=0,
        is_header=True,
        cells=[TableCell(text=normalizer.normalize(c)) for c in header_cells],
    ))
    ridx = 1
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or "|" not in ln:
            break
        cells = _split_row(ln)
        rows.append(TableRow(
            index=ridx,
            is_header=False,
            cells=[TableCell(text=normalizer.normalize(c)) for c in cells],
        ))
        ridx += 1
        i += 1
    src = src_fn()
    el = Element(
        id=make_id(doc_id, "table", src.paragraph_index or 0),
        kind=ElementKind.TABLE,
        source=src,
        section=list(section),
        rows=rows,
        caption="",
    )
    elements.append(el)
    return i


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _build_section_tree(elements: list[Element]) -> list:
    from ..ir import Section

    roots: list[Section] = []
    stack: list[tuple[int, Section]] = []
    counter = 0
    for el in elements:
        if el.kind != ElementKind.HEADING or el.level <= 0:
            continue
        counter += 1
        sec = Section(id=el.id, title=el.text, level=el.level)
        while stack and stack[-1][0] >= el.level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(sec)
        else:
            roots.append(sec)
        stack.append((el.level, sec))
    return roots
