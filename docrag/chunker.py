"""Semantic chunker.

Derives embedding-ready chunks from a :class:`~docrag.ir.Document`. Crucially,
chunking is **per element type**, not a blind character/token slider:

* **Paragraphs** are accumulated into a chunk until the token budget is reached,
  then flushed. A single paragraph larger than the budget is split on Persian
  sentence boundaries (``.`` ``!`` ``؟`` ``…``) so a chunk never starts or ends
  mid-sentence. If a sentence itself is longer than the budget it is hard-split
  on word boundaries as a last resort (and flagged in ``meta``).
* **Lists** are grouped by ``list_id`` and adjacency; items stay whole. A group
  larger than the budget is split between items, never inside one.
* **Tables** become one chunk per table, serialised as ``colname: value`` rows
  (the most retrieval-friendly form). If a *single data row* alone exceeds the
  budget, the table is split row-by-row with the header repeated in every part,
  so each chunk stays self-describing — we never chop a row in two.
* **Footnotes** are emitted one per chunk (they are short and independently
  citable).

Every chunk carries its ``section`` path, the ids of the IR elements it came
from (``source_elements``) and a primary :class:`~docrag.ir.SourceRef`, so a
retrieved chunk can always be traced back to the exact page / paragraph / bbox.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator

from .ids import chunk_id, slugify
from .ir import Document, Element, ElementKind, SourceRef, TableCell, TableRow
from .tokens import count_tokens

# Persian/Latin sentence terminators. Newlines are NOT terminators here (a
# paragraph is already one logical block); only true end-of-sentence marks.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.!?؟…])\s+")
# Word-boundary fallback for a sentence too long on its own.
_WORD_SPLIT_RE = re.compile(r"\s+")
# Soft slide window overlap for paragraph chunks (in sentences) to improve
# recall at boundaries. 0 disables overlap.
_DEFAULT_OVERLAP_SENTENCES = 0


@dataclass
class Chunk:
    """One embedding-ready chunk."""

    id: str
    type: str  # "text" | "list" | "table" | "footnote"
    text: str
    section: list[str] = field(default_factory=list)
    source_elements: list[str] = field(default_factory=list)
    source: SourceRef | None = None
    token_count: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "section": list(self.section),
            "source_elements": list(self.source_elements),
            "source": self.source.to_dict() if self.source else None,
            "token_count": self.token_count,
            "meta": dict(self.meta),
        }


class SemanticChunker:
    """Chunk a :class:`Document` into :class:`Chunk` objects."""

    def __init__(
        self,
        max_tokens: int = 400,
        overlap_sentences: int = _DEFAULT_OVERLAP_SENTENCES,
    ) -> None:
        if max_tokens < 64:
            raise ValueError("max_tokens must be >= 64")
        self.max_tokens = max_tokens
        self.overlap_sentences = max(0, overlap_sentences)

    # -- public ----------------------------------------------------------- #
    def chunk(self, doc: Document) -> list[Chunk]:
        chunks: list[Chunk] = []
        counter = 0

        # Group consecutive textual elements into paragraph/list runs.
        text_buf: list[Element] = []
        current_list_id: str | None = ".__none__"

        def _flush_text() -> None:
            nonlocal counter
            if not text_buf:
                return
            # Split the buffer by list vs paragraph groups for proper handling.
            counter = self._emit_text_group(doc, text_buf, chunks, counter)
            text_buf.clear()

        for el in doc.elements:
            if el.kind == ElementKind.TABLE:
                _flush_text()
                counter = self._emit_table(el, chunks, counter)
                continue
            if el.kind == ElementKind.FOOTNOTE:
                _flush_text()
                counter = self._emit_footnote(el, chunks, counter)
                continue
            if el.kind == ElementKind.LIST_ITEM:
                # Group contiguous items of the same list together.
                lid = el.list_id or ""
                if text_buf and self._last_is_list(text_buf) and lid == current_list_id:
                    text_buf.append(el)
                else:
                    _flush_text()
                    text_buf.append(el)
                    current_list_id = lid
                continue
            # PARAGRAPH / HEADING / CAPTION / TOC_ENTRY
            if text_buf and self._last_is_list(text_buf):
                # switching from list back to prose → flush list first
                _flush_text()
            text_buf.append(el)
            current_list_id = ".__none__"

        _flush_text()
        return chunks

    # -- text groups ------------------------------------------------------ #
    def _emit_text_group(
        self, doc: Document, elements: list[Element],
        chunks: list[Chunk], counter: int,
    ) -> int:
        if not elements:
            return counter
        # If the group is a list run, use list-aware splitting.
        if elements[0].kind == ElementKind.LIST_ITEM:
            return self._emit_list_run(doc, elements, chunks, counter)
        return self._emit_paragraph_run(doc, elements, chunks, counter)

    def _emit_paragraph_run(
        self, doc: Document, elements: list[Element],
        chunks: list[Chunk], counter: int,
    ) -> int:
        section_path: list[str] = []
        source_ids: list[str] = []
        buf_parts: list[str] = []
        buf_tokens = 0

        def _flush(primary: Element | None) -> None:
            nonlocal buf_tokens, counter
            if not buf_parts:
                return
            text = "\n\n".join(buf_parts).strip()
            if not text:
                buf_parts.clear()
                buf_tokens = 0
                return
            counter += 1
            src_el = primary or elements[0]
            chunks.append(
                Chunk(
                    id=chunk_id(doc.id, counter),
                    type="text",
                    text=text,
                    section=list(section_path),
                    source_elements=list(source_ids),
                    source=src_el.source,
                    token_count=count_tokens(text),
                )
            )
            buf_parts.clear()
            source_ids.clear()
            buf_tokens = 0

        for el in elements:
            if el.kind == ElementKind.HEADING:
                # A heading flushes the current prose chunk and seeds context.
                _flush(el)
                section_path = list(el.section)
                # Headings are kept as a tiny context chunk too, so a section
                # title is itself retrievable.
                counter += 1
                chunks.append(
                    Chunk(
                        id=chunk_id(doc.id, counter),
                        type="text",
                        text=el.text,
                        section=list(el.section),
                        source_elements=[el.id],
                        source=el.source,
                        token_count=count_tokens(el.text),
                        meta={"heading": True, "level": el.level},
                    )
                )
                continue

            text = el.text.strip()
            if not text:
                continue
            section_path = list(el.section)
            t_tokens = count_tokens(text)

            if t_tokens <= self.max_tokens:
                if buf_tokens + t_tokens > self.max_tokens:
                    _flush(el)
                buf_parts.append(text)
                source_ids.append(el.id)
                buf_tokens += t_tokens
                # If this single element is already near the budget, flush.
                if buf_tokens >= self.max_tokens:
                    _flush(el)
            else:
                # Paragraph larger than budget: split by sentences.
                _flush(el)
                counter = self._split_long_text(
                    doc, el, text, chunks, counter, kind="text",
                )
        _flush(None)
        return counter

    def _emit_list_run(
        self, doc: Document, elements: list[Element],
        chunks: list[Chunk], counter: int,
    ) -> int:
        section_path: list[str] = []
        buf_lines: list[str] = []
        buf_ids: list[str] = []
        buf_tokens = 0
        primary: Element | None = None

        def _flush() -> None:
            nonlocal buf_tokens, counter, primary
            if not buf_lines:
                return
            text = "\n".join(buf_lines).strip()
            counter += 1
            chunks.append(
                Chunk(
                    id=chunk_id(doc.id, counter),
                    type="list",
                    text=text,
                    section=list(section_path),
                    source_elements=list(buf_ids),
                    source=(primary or elements[0]).source,
                    token_count=count_tokens(text),
                    meta={"item_count": len(buf_lines)},
                )
            )
            buf_lines.clear()
            buf_ids.clear()
            buf_tokens = 0
            primary = None

        for el in elements:
            section_path = list(el.section)
            marker = (el.list_marker or "").strip()
            line = f"{marker} {el.text}".strip() if marker else el.text
            t_tokens = count_tokens(line)
            if buf_tokens + t_tokens > self.max_tokens and buf_lines:
                _flush()
            if primary is None:
                primary = el
            buf_lines.append(line)
            buf_ids.append(el.id)
            buf_tokens += t_tokens
            if buf_tokens >= self.max_tokens:
                _flush()
        _flush()
        return counter

    def _split_long_text(
        self, doc: Document, el: Element, text: str,
        chunks: list[Chunk], counter: int, *, kind: str,
    ) -> int:
        sentences = _split_sentences(text)
        if not sentences:
            return counter
        buf: list[str] = []
        buf_tokens = 0
        overlap = self.overlap_sentences

        def _flush() -> None:
            nonlocal buf_tokens, counter
            if not buf:
                return
            piece = " ".join(buf).strip()
            counter += 1
            chunks.append(
                Chunk(
                    id=chunk_id(doc.id, counter),
                    type=kind,
                    text=piece,
                    section=list(el.section),
                    source_elements=[el.id],
                    source=el.source,
                    token_count=count_tokens(piece),
                    meta={"split": "sentence", "parent": el.id},
                )
            )
            # Keep an overlap window for the next chunk.
            if overlap and len(buf) > overlap:
                buf[:] = buf[-overlap:]
                buf_tokens = sum(count_tokens(s) for s in buf)
            else:
                buf.clear()
                buf_tokens = 0

        for s in sentences:
            s_tokens = count_tokens(s)
            if s_tokens > self.max_tokens:
                # A single sentence longer than the budget: hard word-split.
                _flush()
                counter = self._hard_split_words(doc, el, s, chunks, counter, kind)
                continue
            if buf_tokens + s_tokens > self.max_tokens and buf:
                _flush()
            buf.append(s)
            buf_tokens += s_tokens
            if buf_tokens >= self.max_tokens:
                _flush()
        _flush()
        return counter

    def _hard_split_words(
        self, doc: Document, el: Element, sentence: str,
        chunks: list[Chunk], counter: int, kind: str,
    ) -> int:
        words = _WORD_SPLIT_RE.split(sentence)
        buf: list[str] = []
        buf_tokens = 0
        for w in words:
            wt = count_tokens(w)
            if buf_tokens + wt > self.max_tokens and buf:
                piece = " ".join(buf)
                counter += 1
                chunks.append(
                    Chunk(
                        id=chunk_id(doc.id, counter),
                        type=kind,
                        text=piece,
                        section=list(el.section),
                        source_elements=[el.id],
                        source=el.source,
                        token_count=count_tokens(piece),
                        meta={"split": "word", "parent": el.id, "hard": True},
                    )
                )
                buf.clear()
                buf_tokens = 0
            buf.append(w)
            buf_tokens += wt
        if buf:
            piece = " ".join(buf)
            counter += 1
            chunks.append(
                Chunk(
                    id=chunk_id(doc.id, counter),
                    type=kind,
                    text=piece,
                    section=list(el.section),
                    source_elements=[el.id],
                    source=el.source,
                    token_count=count_tokens(piece),
                    meta={"split": "word", "parent": el.id, "hard": True},
                )
            )
        return counter

    # -- tables ----------------------------------------------------------- #
    def _emit_table(self, el: Element, chunks: list[Chunk], counter: int) -> int:
        rows = el.rows
        if not rows:
            return counter
        ncols = self._col_count(rows)
        headers = self._header_names(rows, ncols)

        # Whole-table token cost (with header).
        full_text = self._table_text(rows, headers, ncols, row_start=0, row_end=len(rows))
        full_tokens = count_tokens(full_text)

        if full_tokens <= self.max_tokens:
            counter += 1
            chunks.append(
                Chunk(
                    id=chunk_id(slugify(el.source.file), counter),
                    type="table",
                    text=full_text,
                    section=list(el.section),
                    source_elements=[el.id],
                    source=el.source,
                    token_count=full_tokens,
                    meta={
                        "table_id": el.id,
                        "row_count": len(rows),
                        "col_count": ncols,
                    },
                )
            )
            return counter

        # Table too big: split by rows, header repeated in each part.
        header_tokens = count_tokens(self._header_block(headers))
        per_row_tokens = [
            count_tokens(self._row_block(r, headers, ncols)) for r in rows
        ]
        # Always include header row (rows[0]) in every part.
        header_row = rows[0] if rows[0].is_header else None

        buf_rows: list[TableRow] = []
        buf_tokens = header_tokens
        parts: list[list[TableRow]] = []

        for r in rows:
            rt = per_row_tokens[r.index]
            if rt + header_tokens > self.max_tokens:
                # A single row exceeds budget even with just the header:
                # emit it alone (cell-level split would lose the row's meaning;
                # we keep the row whole and flag it as oversized).
                if buf_rows:
                    parts.append(buf_rows)
                    buf_rows = []
                    buf_tokens = header_tokens
                parts.append([r])
                continue
            if buf_tokens + rt > self.max_tokens and buf_rows:
                parts.append(buf_rows)
                buf_rows = []
                buf_tokens = header_tokens
            buf_rows.append(r)
            buf_tokens += rt
        if buf_rows:
            parts.append(buf_rows)

        for part in parts:
            text = self._table_text_for_part(part, headers, ncols, header_row)
            counter += 1
            chunks.append(
                Chunk(
                    id=chunk_id(slugify(el.source.file), counter),
                    type="table",
                    text=text,
                    section=list(el.section),
                    source_elements=[el.id],
                    source=el.source,
                    token_count=count_tokens(text),
                    meta={
                        "table_id": el.id,
                        "row_count": len(part),
                        "col_count": ncols,
                        "split": "row",
                    },
                )
            )
        return counter

    def _emit_footnote(self, el: Element, chunks: list[Chunk], counter: int) -> int:
        text = el.text.strip()
        if not text:
            return counter
        if count_tokens(text) > self.max_tokens:
            counter = self._split_long_text(
                _StubDoc(el), el, text, chunks, counter, kind="footnote"
            )
            return counter
        counter += 1
        chunks.append(
            Chunk(
                id=chunk_id(slugify(el.source.file), counter),
                type="footnote",
                text=text,
                section=list(el.section),
                source_elements=[el.id],
                source=el.source,
                token_count=count_tokens(text),
                meta={"footnote_id": el.meta.get("footnote_id")},
            )
        )
        return counter

    # -- table helpers ---------------------------------------------------- #
    @staticmethod
    def _col_count(rows: list[TableRow]) -> int:
        return max((sum(c.col_span for c in r.cells) for r in rows), default=0)

    @staticmethod
    def _header_names(rows: list[TableRow], ncols: int) -> list[str]:
        names = [f"ستون {i + 1}" for i in range(ncols)]
        if not rows:
            return names
        header_row = rows[0] if rows[0].is_header else None
        if header_row is None:
            return names
        col = 0
        for cell in header_row.cells:
            for _ in range(cell.col_span):
                if col < ncols:
                    txt = cell.text.strip()
                    if txt:
                        names[col] = txt
                    col += 1
        return names

    @staticmethod
    def _cell_columns(row: TableRow) -> list[tuple[int, int, str]]:
        """Return ``[(start_col, span, text), ...]`` for a row's origin cells."""
        out: list[tuple[int, int, str]] = []
        col = 0
        for cell in row.cells:
            out.append((col, cell.col_span, cell.text))
            col += cell.col_span
        return out

    @staticmethod
    def _header_block(headers: list[str]) -> str:
        return "ستون‌ها: " + " | ".join(headers)

    def _row_block(self, row: TableRow, headers: list[str], ncols: int) -> str:
        lines: list[str] = []
        for start, span, text in self._cell_columns(row):
            for k in range(span):
                idx = start + k
                if idx < len(headers):
                    lines.append(f"{headers[idx]}: {text.strip()}")
        return "\n".join(lines)

    def _table_text(
        self, rows: list[TableRow], headers: list[str], ncols: int,
        *, row_start: int, row_end: int,
    ) -> str:
        parts: list[str] = []
        if rows and rows[0].is_header:
            parts.append(self._header_block(headers))
            body = rows[1:row_end] if row_start == 0 else rows[row_start:row_end]
        else:
            body = rows[row_start:row_end]
        for r in body:
            parts.append(self._row_block(r, headers, ncols))
        return "\n\n".join(p for p in parts if p)

    def _table_text_for_part(
        self, part: list[TableRow], headers: list[str], ncols: int,
        header_row: TableRow | None,
    ) -> str:
        parts: list[str] = [self._header_block(headers)]
        for r in part:
            if r is header_row:
                continue
            parts.append(self._row_block(r, headers, ncols))
        return "\n\n".join(p for p in parts if p)

    # -- misc ------------------------------------------------------------- #
    @staticmethod
    def _last_is_list(buf: list[Element]) -> bool:
        return bool(buf) and buf[-1].kind == ElementKind.LIST_ITEM


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` on sentence terminators, keeping terminators attached."""
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


class _StubDoc:
    """Minimal stand-in giving ``_split_long_text`` the ``.id`` it needs."""

    def __init__(self, el: Element) -> None:
        self.id = el.source.file


def chunk_document(
    doc: Document, max_tokens: int = 400, overlap_sentences: int = 0,
) -> list[Chunk]:
    """Convenience wrapper around :class:`SemanticChunker`."""
    return SemanticChunker(max_tokens=max_tokens, overlap_sentences=overlap_sentences).chunk(doc)


def chunks_to_dicts(chunks: Iterable[Chunk]) -> list[dict[str, Any]]:
    return [c.to_dict() for c in chunks]
