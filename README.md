# docrag — Persian Document Preprocessing Pipeline for RAG

`docrag` turns **PDF, DOCX, XLSX and URL/HTML** sources (with a focus on
**Persian / Farsi** content) into a structured, traceable intermediate
representation (IR) and from it derives three artifacts: a **JSON IR**, an
**HTML preview** for visual QA, and **embedding-ready chunks**. It is
deliberately decoupled from any vector database: the IR is the single source of
truth, and chunks/HTML are derived from it so that evidence can always be
reconstructed and validated before anything is embedded.

A small field-aware :class:`~docrag.retriever.Retriever` is included to
acceptance-test that the chunks carry enough structure for exact-value answers
(e.g. "سود دوره ۱۴۰۰ چقدر بوده؟" answered from the right cell, not a fuzzy match).

> Designed as the ingestion stage for Hoshi-Bot (or any Persian RAG stack).
> The IR JSON plugs directly into an embedding step; nothing here depends on a
> specific vector store.

---

## Architecture

```
        PDF / DOCX / XLSX / URL
                  │
   ┌──────┬───────┼────────┬────────┐
   ▼      ▼       ▼        ▼        ▼
 PDF    DOCX    XLSX      URL     (shared)     ← Parser Layer
Parser  Parser  Parser   Parser   Normalizer
(pdfplumber (python-docx (openpyxl: (link2rag port
 +pypdfium2  + numbering  table     → markdownify,
 +tables +   XML + merge  detect +  bs4 dedup +
 reading     cells +       multi-   front-matter)
 order)      footnotes)    level hdr
                                        + merged + formula)
   └──────┴───────┼────────┴────────┘
                  ▼
        Document IR (JSON)             ← file-type-independent model
   sections / paragraphs / lists /
   tables (with spans + cell addresses) /
   footnotes / source refs
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
   Chunker   Renderer  Validator     ← derived artefacts
   (semantic  (HTML QA) (schema +
    per-type)            refs + quality)
        │
        ▼
   chunks JSON  ──►  Embedding (your stack)
        │
        ▼
   Retriever (field-aware, optional)  ← acceptance-test exact answers
```

**Four layers** (each replaceable independently):

| Layer | Module | Responsibility |
|---|---|---|
| Parser | `docrag.parsers` | PDF / DOCX / XLSX / URL → IR (one parser per source type) |
| Document IR | `docrag.ir` | File-type-independent structured model |
| Semantic Chunker | `docrag.chunker` | Per-type chunking (text / list / table / footnote) |
| Evaluation | `docrag.validator` | Schema + reference integrity + quality metrics |

Supporting modules: `docrag.normalizer` (full Persian normalisation + search
keys), `docrag.renderer` (HTML QA), `docrag.pipeline` (orchestrator),
`docrag.retriever` (field-aware exact-value retriever over chunks),
`docrag.cli` (Click CLI), `docrag.api` (FastAPI REST).

---

## Install

Python 3.10+.

```bash
pip install -e .            # core: parsers + chunker + renderer + CLI
pip install -e ".[api]"     # + FastAPI REST server
pip install -e ".[dev]"     # + pytest for running the test-suite
pip install -e ".[all]"     # everything
```

This installs the `docrag` command-line tool and an importable `docrag` package.

---

## Quick start

### CLI

```bash
# Full pipeline on one file → IR JSON, HTML, chunks JSON, validation report
docrag process input/contract.docx --output output --max-tokens 400

# XLSX and URL/HTML are supported with the same command
docrag process input/صورت_مالی.xlsx --output output
docrag process https://example.com/fa --output output
docrag process input/page.html --output output

# Whole directory of PDFs/DOCX/XLSX/HTML
docrag process input/ --output output --max-tokens 400

# Re-derive chunks from an existing IR (no re-parsing)
docrag chunk output/contract.json --output chunks.json --max-tokens 500

# Validate an IR (+ optional chunks)
docrag validate output/contract.json --chunks output/contract.chunks.json

# Quick text normalisation
docrag normalize --text "مي‌كند و كتاب ها" --digits persian

# Render an IR to HTML for visual review
docrag render output/contract.json --output preview.html

# Run the REST API
docrag serve --port 8080
```

Options: `--digits {persian,latin,keep}` (default `persian`), `--overlap N`
(sentence overlap between consecutive text chunks).

### Python API

```python
from docrag import Pipeline

pipe = Pipeline(max_tokens=400)
result = pipe.process("input/contract.docx", "output/")

print(len(result.document), "elements →", len(result.chunks), "chunks")
print("validation:", "OK" if result.ok else "FAIL")
# Outputs written: output/contract.{json,html,chunks.json,report.json}
```

Lower-level usage (parse only, chunk separately):

```python
from docrag.parsers import get_parser
from docrag.chunker import SemanticChunker
from docrag.renderer import render_html
from docrag.validator import validate

doc = get_parser("input/report.pdf").parse("input/report.pdf")
chunks = SemanticChunker(max_tokens=400).chunk(doc)
html = render_html(doc)
report = validate(doc, chunks)
```

### REST API

```bash
curl -F "file=@contract.docx" "http://localhost:8080/process?max_tokens=400" \
  | jq '.document, .chunks, .report'
```

Endpoints: `GET /healthz`, `POST /process` (upload file → IR+chunks+report),
`POST /chunk` (IR JSON → chunks), `POST /normalize` (text → normalised text).

---

## Excel & URL ingestion

### XLSX — Excel-aware parsing

A spreadsheet is not flat text: "سود دوره ۱۴۰۰ چقدر بوده؟" must be answered from
the **cell** that holds the profit for 1400, not from a fuzzy text match. The
XLSX parser (`docrag.parsers.xlsx_parser`) therefore:

- **Detects tables** by blank-row/blank-column splitting (merged-aware, so a
  merged title or parent header is not mistaken for a gap).
- **Combines multi-level headers**: a parent header merged across columns plus
  its child row collapses to one header per column (`درآمد` + `کل` → `درآمد کل`).
- **Propagates merged data cells** to each spanned row so every chunk is
  self-contained, while the anchor cell keeps its value; the chunk's
  `cell_refs` map points each header back to the anchor cell address.
- **Handles formulas**: the workbook is opened twice (normal + `data_only`). A
  formula with a cached result yields that result; a formula without one is
  flagged `needs_review` and the formula string never reaches a chunk.
- **Keeps identifiers exact**: phone numbers, codes and IDs stay as strings
  (leading zeros survive) and `آ` is not folded to `ا` in cell display.
- **Tracks hidden rows/filters** in metadata instead of dropping them.

Each data row is emitted as its own `TABLE` element (header row + that one data
row), so the chunker produces one self-contained chunk per row carrying a
`colname: value` body and a `cell_refs` map (`{"سود": "F4"}`) for citation.

### URL / HTML — link2rag port (permissive)

The URL parser (`docrag.parsers.url_parser`) ingests a web page (or a local
`.html` file) through the `link2rag` approach, ported to permissive
dependencies: fetch with `urllib` → strip non-content DOM with BeautifulSoup →
de-duplicate the mobile/desktop duplicate blocks WordPress/Elementor and Next.js
render → convert to Markdown with **markdownify** (MIT) → parse into IR
(headings, paragraphs, lists, pipe tables) with a YAML front-matter block
carrying title / source URL / lang / description.

> **Why not `html2text`?** The original `link2rag` used `html2text` for the
> HTML→Markdown step, but `html2text` is **GPL-3.0** — a copyleft license that
> would force any web service linking it to also publish its source. docrag
> swaps it for `markdownify` (MIT); the de-duplication, filtering and
> post-processing logic is preserved unchanged.

Optional dynamic rendering (accordion expansion, scroll-based hydration) shells
out to the `agent-browser` CLI when present — it is **not** a Python dependency
and is silently skipped when the tool is absent.

```python
from docrag import Pipeline, Retriever

pipe = Pipeline()
# One pipeline, four source types:
chunks = []
for src in ["report.pdf", "contract.docx", "صورت_مالی.xlsx", "https://site.ir/fa"]:
    chunks.extend(pipe.run(src).chunks)

ret = Retriever(chunks)
ans = ret.query("سود دوره ۱۴۰۰ چقدر بوده؟")
print(ans.value)        # "۴٬۰۰۰٬۰۰۰٬۰۰۰"
print(ans.source)       # {filename, sheet, row, cell: "F4", range}
print(ans.needs_review) # False (the formula had a cached value)
```

---

## Output formats

For an input `contract.docx`, `docrag process` writes four files:

| File | Purpose |
|---|---|
| `contract.json` | The IR — structured document model + source references |
| `contract.html` | Self-contained RTL HTML for visual quality review |
| `contract.chunks.json` | Embedding-ready chunks with traceability |
| `contract.report.json` | Validation report (errors/warnings/metrics) |

### IR JSON (excerpt)

```json
{
  "id": "contract", "source_file": "contract.docx", "file_type": "docx",
  "schema_version": "0.2",
  "elements": [
    {"id": "contract.docx::p0::heading", "type": "heading", "level": 1,
     "text": "قرارداد همکاری خدمات نرم‌افزاری",
     "section": [], "source": {"file": "contract.docx", "paragraph_index": 0}},
    {"id": "contract.docx::tbl::0", "type": "table",
     "rows": [{"index": 0, "is_header": true,
               "cells": [{"text": "ردیف", "row_span": 1, "col_span": 1}, …]}],
     "source": {"file": "contract.docx", "paragraph_index": 12}}
  ],
  "sections": [{"id": "…", "title": "تعهدات مالی", "level": 1, "children": […]}]
}
```

### Chunk JSON (excerpt)

A table becomes a self-describing `colname: value` block — never split mid-row:

```json
{
  "id": "contract::chunk::0007", "type": "table",
  "text": "ستون‌ها: سال | درآمد | هزینه | سود\n\nسال: ۱۴۰۳\nدرآمد: ۱۲٬۰۰۰٬۰۰۰٬۰۰۰\n…",
  "section": ["گزارش مالی", "عملکرد سالانه"],
  "source_elements": ["contract.docx::tbl::0"],
  "source": {"file": "report.pdf", "page": 12},
  "token_count": 364
}
```

Every chunk carries `section`, `source_elements` (IR element ids it came from)
and a primary `source` (file + page/bbox or paragraph) so a retrieved chunk can
always be traced back to the exact origin.

---

## Why a Persian-specific pipeline?

Persian text degrades retrieval precision silently unless normalised:

- Arabic `ي`/`ك` vs Persian `ی`/`ک` → same word, two distinct tokens.
- `می کند` (plain space) vs `می‌کند` (ZWNJ) → no match.
- Arabic-Indic digits `٠١` vs Persian digits `۰۱` → numbers don't match.

`docrag.normalizer` fixes all of these (configurable digit handling), and the
PDF parser reads each table cell via PDFium's `get_text_bounded`, which applies
the Unicode bidi algorithm so RTL text comes out in **logical order**
(`سال`, not the reversed `لاس` that a naive left-to-right char sweep produces).

---

## Chunking strategy

Chunking is **per element type**, not a blind token slider:

- **Paragraphs** accumulate until the budget, then flush. A paragraph larger
  than the budget is split on Persian sentence boundaries (`. ! ؟ …`); an
  over-long sentence is hard-split on words as a last resort.
- **Lists** group contiguous items of the same list, keeping items whole.
- **Tables** serialise as `colname: value` rows. If a single row alone exceeds
  the budget, the table splits **by rows with the header repeated** in every
  part — a row is never chopped in two.
- **Footnotes** emit one chunk each (short, independently citable).

Merged table cells are preserved: DOCX `gridSpan`/`vMerge` are read directly
from the XML so `row_span`/`col_span` survive; PDF tables (no merge info) get
span-1 cells.

---

## Validation & quality report

`docrag.validate` checks:

- Every element has a valid kind, unique id, and a source pointing at the right
  file (page for PDF, paragraph_index for DOCX).
- Every chunk's `source_elements` actually exist; no empty chunk text; oversize
  chunks flagged.
- Metrics: element counts, empty-textual elements, heading-level distribution,
  table row counts, chunk token min/max/mean, and **chunk coverage** (fraction
  of IR elements covered by at least one chunk).

Run `docrag validate …` or read `*.report.json`.

---

## Test suite

```bash
pip install -e ".[dev]"
pytest                  # 111 tests
```

Fixtures live in `fixtures/`: a Persian legal contract DOCX (merged cells,
lists, footnotes), a Persian financial-report PDF (table + headings), two
Persian XLSX workbooks (an agencies list with a merged `منطقه` column + hidden
row, and a multi-level-header financial statement with `سود` formulas recalced
via LibreOffice and `نرخ رشد` formulas stripped to exercise the
`needs_review` path), and a Persian sample HTML page (headings, table, list,
mobile/desktop duplicate footer). Regenerate them with
`python scripts/build_fixtures.py` (requires LibreOffice `soffice` for the
DOCX→PDF conversion and the XLSX formula recalc).

---

## Limitations (v3)

| Capability | Status |
|---|---|
| DOCX heading extraction | ✅ implemented |
| DOCX table extraction (incl. merged cells) | ✅ implemented (XML-direct) |
| DOCX list numbering (multi-level + restart) | ✅ implemented |
| DOCX footnotes | ✅ implemented |
| PDF table detection | ✅ implemented |
| PDF table cell text (RTL-correct) | ✅ implemented (PDFium bidi reordering) |
| PDF reading order | ~ approximate (2-column heuristic) |
| PDF heading detection | ~ font-size heuristic |
| **XLSX table detection** | ✅ implemented (blank-row/col split, merged-aware) |
| **XLSX multi-level headers** | ✅ implemented (parent+child combination) |
| **XLSX merged cells** | ✅ implemented (propagated to rows, anchor preserved) |
| **XLSX formulas** | ✅ cached value used; missing → `needs_review` |
| **URL/HTML → Markdown → IR** | ✅ implemented (link2rag port, markdownify) |
| **URL dynamic rendering** | ~ optional via `agent-browser` CLI (not a Python dep) |
| OCR of scanned pages | ❌ not in scope |
| PDF merged-cell detection | ❌ pdfplumber gives no merge info → span-1 |
| Equation extraction | ❌ not yet |
| Retrieval evaluation | ❌ not yet (Retriever is for acceptance testing) |

The IR JSON is intentionally **not** wired straight into embedding: validate
and reconstruct evidence first, then embed.

---

## Project layout

```
docrag/
├── pyproject.toml
├── README.md
├── docrag/
│   ├── ir.py              # Document IR (dataclasses)
│   ├── normalizer.py      # Persian normaliser + search keys
│   ├── ids.py             # stable id generation
│   ├── tokens.py          # tiktoken token counting
│   ├── chunker.py         # semantic chunker (table cell_refs/needs_review)
│   ├── renderer.py        # HTML renderer
│   ├── validator.py       # validation + quality report
│   ├── retriever.py       # field-aware retriever (acceptance testing)
│   ├── pipeline.py        # orchestrator (URL + multi-type process_dir)
│   ├── cli.py             # Click CLI
│   ├── api.py             # FastAPI REST
│   └── parsers/
│       ├── base.py
│       ├── docx_parser.py
│       ├── pdf_parser.py
│       ├── xlsx_parser.py     # openpyxl: tables, multi-level hdr, merged, formula
│       ├── url_parser.py      # URL/local HTML → IR
│       ├── html_to_md.py      # link2rag port (markdownify, not GPL html2text)
│       └── md_to_ir.py        # lightweight Markdown → IR
├── tests/                 # pytest suite (111 tests)
├── fixtures/              # contract.docx, financial_report.pdf,
│                          # نمایندگی‌ها.xlsx, صورت_مالی.xlsx, sample_page.html
├── scripts/build_fixtures.py
└── examples/demo.py
```

## License

MIT.

### Dependency licensing (commercial / SaaS-safe)

docrag and every runtime dependency is **permissively licensed** — no GPL,
LGPL, or AGPL component, so the package is safe to embed in a closed-source
commercial online service (e.g. a chatbot) with no source-disclosure
obligation.

| Dependency | Purpose | License |
|---|---|---|
| `pdfplumber` | PDF page geometry, character data, table structure | MIT |
| `pypdfium2` | PDF text extraction with native bidi reordering (wraps Google PDFium) | BSD-3-Clause / Apache-2.0 |
| `python-docx` | DOCX parsing | MIT |
| `openpyxl` | XLSX parsing (workbook, merged cells, formulas, formats) | MIT |
| `beautifulsoup4` | HTML DOM parsing + de-duplication for URL ingestion | MIT |
| `markdownify` | HTML → Markdown for URL ingestion | MIT |
| `tiktoken` | Token counting for chunk sizing | MIT |
| `click` | CLI | BSD-3-Clause |
| `pydantic` | IR validation | MIT |

> **Why not PyMuPDF?** Earlier versions used PyMuPDF for PDF parsing, but
> PyMuPDF is **AGPL-3.0**: its network-use clause (Section 13) would force any
> service that lets users interact with it over the network to release its
> entire source tree under a compatible license. That is incompatible with a
> closed-source commercial product, so PyMuPDF was removed in favour of
> pdfplumber + pypdfium2, which produce equivalent results (including correct
> Persian RTL ordering via PDFium's bidi implementation) under permissive
> licenses.

> **Why not `html2text` for URLs?** The original `link2rag` used `html2text`
> for HTML→Markdown, but `html2text` is **GPL-3.0** — the same copyleft problem
> as PyMuPDF. docrag uses `markdownify` (MIT) instead, preserving the
> retrieval-friendly de-duplication and post-processing logic. (LibreOffice,
> MPL-2.0, is used only as a build-time tool to recalc the sample XLSX formulas
> and to convert the sample DOCX→PDF; it never enters the runtime tree.)
