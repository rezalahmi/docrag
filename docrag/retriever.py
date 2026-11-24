"""Field-aware retriever over docrag chunks.

docrag is a *preprocessing* pipeline: it produces chunks, not answers. This
retriever is a small, deterministic layer that sits on top of the chunk store
to demonstrate — and acceptance-test — that the chunks actually carry enough
structure to answer exact-value questions such as "سود دوره ۱۴۰۰ چقدر بوده؟"
from the right cell, not from a fuzzy text match.

It works on the ``colname: value`` text the chunker emits for table chunks
plus the ``cell_refs`` / ``needs_review`` metadata, so it needs no extra
storage beyond the chunks themselves. In production the same chunk store
would feed an embedding model; the field-aware extraction layer above the
embeddings is what guarantees exact cell answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field, field
from typing import Any

from .chunker import Chunk
from .normalizer import Normalizer

_STOPWORDS = {
    "چیست", "چنده", "چقدر", "بوده", "است", "در", "به", "از", "که", "و", "یا",
    "کدام", "کدامیک", "چه", "هایی", "های", "ها", "را", "برای", "هست", "هستند",
    "شد", "شده", "می", "کنید", "please", "what", "how", "much", "is", "the",
    "of", "in", "which", "?", "؟", ".", "،",
}
_SHEET_Q = re.compile(r"کدام\s*sheet|کدام\s*صفحه", re.IGNORECASE)
_FILE_Q = re.compile(r"کدام\s*فایل|در\s*کدام\s*فایل", re.IGNORECASE)
_KV_LINE = re.compile(r"^([^:]+):\s*(.+)$")


@dataclass
class Answer:
    question: str
    value: str
    found: bool
    source: dict[str, Any]
    chunk_id: str
    field: str | None = None
    needs_review: bool = False
    candidates: list[dict[str, Any]] = dc_field(default_factory=list)


@dataclass
class _View:
    chunk: Chunk
    fields: dict[str, str]
    search_fields: dict[str, str]
    cell_refs: dict[str, str]
    needs_review: set[str]
    section: list[str]
    source_file: str
    search_text: str


class Retriever:
    """Field-aware retriever over a list of :class:`~docrag.chunker.Chunk`."""

    def __init__(self, chunks: list[Chunk], normalizer: Normalizer | None = None) -> None:
        self.norm = normalizer or Normalizer()
        self.views = [self._view(c) for c in chunks]

    # -- public ----------------------------------------------------------- #
    def query(self, question: str) -> Answer:
        q = question.strip()
        if _SHEET_Q.search(q) and not _FILE_Q.search(q):
            return self._answer_sheet(q)
        if _FILE_Q.search(q) and ("سود" in q or "درآمد" in q or "هزینه" in q):
            return self._answer_value_with_file(q)
        return self._answer_field_value(q)

    # -- view construction ----------------------------------------------- #
    def _view(self, c: Chunk) -> _View:
        fields: dict[str, str] = {}
        for line in c.text.split("\n"):
            m = _KV_LINE.match(line.strip())
            if not m:
                continue
            key = m.group(1).strip()
            if key == "ستون‌ها":
                continue
            fields[key] = m.group(2).strip()
        search_fields = {k: self.norm.numeric_search_key(v) for k, v in fields.items()}
        cell_refs = dict(c.meta.get("cell_refs") or {})
        needs_review = set(c.meta.get("needs_review") or [])
        return _View(
            chunk=c,
            fields=fields,
            search_fields=search_fields,
            cell_refs=cell_refs,
            needs_review=needs_review,
            section=list(c.section),
            source_file=(c.source.file if c.source else ""),
            search_text=self.norm.search_key(c.text),
        )

    # -- "کدام Sheet ..." ------------------------------------------------- #
    def _answer_sheet(self, question: str) -> Answer:
        kw = self._content_tokens(question)
        best = None
        best_score = -1
        for v in self.views:
            sheet = v.section[0] if v.section else ""
            table = v.section[1] if len(v.section) > 1 else sheet
            sk = self.norm.search_key(sheet)
            tk_ = self.norm.search_key(table)
            score = 0
            for t in kw:
                tk = self.norm.search_key(t)
                if tk and tk in sk:
                    score += 3
                if tk and tk in tk_:
                    score += 2
                if tk and tk in v.search_text:
                    score += 1
            if score > best_score:
                best_score = score
                best = v
        if best is None or best_score <= 0:
            return Answer(question=question, value="", found=False, source={},
                          chunk_id="", field=None)
        sheet = best.section[0] if best.section else ""
        return Answer(
            question=question, value=sheet, found=True,
            source={"filename": best.source_file, "sheet": sheet},
            chunk_id=best.chunk.id, field="sheet",
        )

    # -- "... در کدام فایل ..." ------------------------------------------- #
    def _answer_value_with_file(self, question: str) -> Answer:
        base = self._answer_field_value(question)
        if not base.found:
            return base
        v = next((x for x in self.views if x.chunk.id == base.chunk_id), None)
        cell = ""
        if v and base.field:
            cell = v.cell_refs.get(base.field, "")
        base.source = {
            "filename": v.source_file if v else "",
            "sheet": v.section[0] if v and v.section else "",
            "row": (v.chunk.source.paragraph_index if v and v.chunk.source else None),
            "cell": cell,
            "range": (v.chunk.meta.get("range") if v else ""),
        }
        base.value = (
            f"فایل: {base.source['filename']} — Sheet: {base.source['sheet']} — "
            f"سلول: {cell}"
        )
        return base

    # -- field-value answer ---------------------------------------------- #
    def _answer_field_value(self, question: str) -> Answer:
        tokens = self._content_tokens(question)
        headers = self._ordered_headers()
        header_tokens = self._header_like_tokens(tokens, headers)
        target = header_tokens[0][2] if header_tokens else None
        filters, free_values = self._extract_filters(tokens, header_tokens, target)

        best = None
        best_score = -1.0
        for v in self.views:
            score = self._score(v, target, filters, free_values)
            if score > best_score:
                best_score = score
                best = v
        if best is None or best_score <= 0:
            return Answer(question=question, value="", found=False, source={},
                          chunk_id="", field=None)
        field = target
        if field is None or field not in best.fields:
            # The target header was guessed from the global header set, which
            # may differ slightly from the winning chunk's headers (e.g. the
            # PDF has an exact "درآمد" column while the XLSX has "درآمد کل").
            # Fall back to the field whose header best matches the target by
            # containment, not to the first non-empty field.
            field = self._closest_field(target, best.fields)
        if field is None:
            field = next((h for h in best.fields if best.fields[h]), None)
        if field is None:
            return Answer(question=question, value="", found=False, source={},
                          chunk_id=best.chunk.id, field=None)
        cell = best.cell_refs.get(field, "")
        src = best.chunk.source
        return Answer(
            question=question,
            value=best.fields[field],
            found=True,
            source={
                "filename": best.source_file,
                "sheet": best.section[0] if best.section else "",
                "table": best.section[1] if len(best.section) > 1 else "",
                "row": (src.paragraph_index if src else None),
                "cell": cell,
                "range": best.chunk.meta.get("range", ""),
            },
            chunk_id=best.chunk.id,
            field=field,
            needs_review=field in best.needs_review,
        )

    # -- tokenisation / field guessing ----------------------------------- #
    def _content_tokens(self, question: str) -> list[str]:
        toks = re.findall(r"[\w\u0600-\u06FF]+", question)
        return [t for t in toks
                if self.norm.search_key(t) not in _STOPWORDS and len(t) > 1]

    def _ordered_headers(self) -> list[str]:
        seen: list[str] = []
        sset: set[str] = set()
        for v in self.views:
            for h in v.fields:
                if h not in sset:
                    sset.add(h)
                    seen.append(h)
        return seen

    def _closest_field(self, target: str | None, fields: dict[str, str]) -> str | None:
        """Pick the field whose header best matches ``target`` by containment.

        Used when the target header (guessed globally) is not an exact key in
        the winning chunk's fields — e.g. target ``"درآمد"`` resolving to
        ``"درآمد کل"``. Prefers an exact key, then a header that contains the
        target (or vice-versa), longest match first.
        """
        if not target or not fields:
            return None
        tk = self.norm.search_key(target)
        if not tk:
            return None
        best: str | None = None
        best_len = 0
        for h in fields:
            if not fields[h]:
                continue
            hk = self.norm.search_key(h)
            if not hk:
                continue
            if tk == hk:
                return h
            if tk in hk or hk in tk:
                ln = min(len(tk), len(hk))
                if ln > best_len:
                    best_len = ln
                    best = h
        return best

    def _header_like_tokens(self, tokens, headers) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for i, t in enumerate(tokens):
            tk = self.norm.search_key(t)
            if not tk:
                continue
            exact = None
            partial = None
            for h in headers:
                hk = self.norm.search_key(h)
                if not hk:
                    continue
                if tk == hk:
                    exact = h
                    break
                if partial is None and (tk in hk or hk in tk):
                    partial = h
            m = exact or partial
            if m is not None:
                out.append((i, t, m))
        return out

    def _extract_filters(self, tokens, header_tokens, target):
        # Every header-like position (including the target's) is reserved: the
        # target token must not also count as a free value, or every chunk that
        # merely mentions it gets a spurious +0.25 from the text fallback.
        header_positions = {p for p, _, _ in header_tokens}
        used: set[int] = set(header_positions)
        filters: list[tuple[str, str]] = []
        for pos, _, header in header_tokens:
            if header == target:
                continue
            for j in range(pos + 1, len(tokens)):
                if j in used:
                    continue
                filters.append((header, tokens[j]))
                used.add(j)
                break
        free_values = [tokens[j] for j in range(len(tokens)) if j not in used]
        return filters, free_values

    def _score(self, v: _View, target, filters, free_values) -> float:
        score = 0.0
        if target and target in v.fields and v.fields[target]:
            score += 1.0
        for header, value in filters:
            hk = self.norm.search_key(header)
            vk_num = self.norm.numeric_search_key(value)
            vk_txt = self.norm.search_key(value)
            matched = False
            for h, sv in v.search_fields.items():
                if hk and hk in self.norm.search_key(h):
                    if vk_num and vk_num in self.norm.numeric_search_key(sv):
                        score += 3.0
                        matched = True
                        break
                    if vk_txt and vk_txt in sv:
                        score += 2.0
                        matched = True
                        break
            if not matched:
                if (vk_num and vk_num in self.norm.numeric_search_key(v.chunk.text)) or (
                    vk_txt and vk_txt in v.search_text
                ):
                    score += 0.5
        for value in free_values:
            vk_num = self.norm.numeric_search_key(value)
            vk_txt = self.norm.search_key(value)
            hit = False
            for sv in v.search_fields.values():
                if vk_num and vk_num in self.norm.numeric_search_key(sv):
                    score += 2.0
                    hit = True
                    break
                if vk_txt and vk_txt in sv:
                    score += 1.5
                    hit = True
                    break
            if not hit:
                if (vk_num and vk_num in self.norm.numeric_search_key(v.chunk.text)) or (
                    vk_txt and vk_txt in v.search_text
                ):
                    score += 0.25
        if v.chunk.meta.get("hidden"):
            score -= 0.25
        return score
