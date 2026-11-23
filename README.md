# docrag — Persian Document Preprocessing Pipeline for RAG

`docrag` turns PDF and DOCX files (with a focus on **Persian / Farsi** content)
into a structured, traceable intermediate representation (IR) and from it
derives three artifacts: a **JSON IR**, an **HTML preview** for visual QA, and
**embedding-ready chunks**. It is deliberately decoupled from any vector
database: the IR is the single source of truth, and chunks/HTML are derived
from it so that evidence can always be reconstructed and validated before
anything is embedded.

> Designed as the ingestion stage for Hoshi-Bot (or any Persian RAG stack).
> The IR JSON plugs directly into an embedding step; nothing here depends on a
> specific vector store.

---

## Architecture

```
            PDF / DOCX
                │
        ┌───────┴────────┐
        ▼                ▼
   PDF Parser       DOCX Parser        ← Parser Layer
   (PyMuPDF +       (python-docx +
    tables +         numbering XML +
    reading order)   merge cells)
        │                │
        └───────┬────────┘
                ▼
        Document IR (JSON)             ← file-type-independent model
   sections / paragraphs / lists /
   tables (with spans) / footnotes /
   source refs (page + bbox / paragraph)
                │
        ┌───────┼───────┐
        ▼       ▼       ▼
   Chunker  Renderer  Validator       ← derived artefacts
   (semantic, (HTML QA) (schema +
    per-type)            refs + quality)
        │
        ▼
   chunks JSON  ──►  Embedding (your stack)
```

**Four layers** (each replaceable independently):

| Layer | Module | Responsibility |
|---|---|---|
| Parser | `docrag.parsers` | PDF (PyMuPDF) / DOCX (python-docx) → IR |
| Document IR | `docrag.ir` | File-type-independent structured model |
| Semantic Chunker | `docrag.chunker` | Per-type chunking (text / list / table / footnote) |
| Evaluation | `docrag.validator` | Schema + reference integrity + quality metrics |

Supporting modules: `docrag.normalizer` (full Persian normalisation),
`docrag.renderer` (HTML QA), `docrag.pipeline` (orchestrator), `docrag.cli`
(Click CLI), `docrag.api` (FastAPI REST).

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

# Whole directory of PDFs/DOCXs
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
PDF parser extracts table cells via clip regions so RTL text comes out in
**logical order** (PyMuPDF's `extract()` returns it reversed for Persian).

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
pytest                  # 75 tests
```

Fixtures live in `fixtures/` (a Persian legal contract DOCX with merged cells,
lists and footnotes; a Persian financial-report PDF with a table). Regenerate
them with `python scripts/build_fixtures.py` (requires LibreOffice `soffice`
for the DOCX→PDF conversion).

---

## Limitations (v2)

| Capability | Status |
|---|---|
| DOCX heading extraction | ✅ implemented |
| DOCX table extraction (incl. merged cells) | ✅ implemented (XML-direct) |
| DOCX list numbering (multi-level + restart) | ✅ implemented |
| DOCX footnotes | ✅ implemented |
| PDF table detection | ✅ implemented |
| PDF table cell text (RTL-correct) | ✅ implemented (clip extraction) |
| PDF reading order | ~ approximate (2-column heuristic) |
| PDF heading detection | ~ font-size heuristic |
| OCR of scanned pages | ❌ not in v2 (removed by design) |
| PDF merged-cell detection | ❌ PyMuPDF gives no merge info → span-1 |
| Equation extraction | ❌ not yet |
| Retrieval evaluation | ❌ not yet (evaluation layer = validation + fixtures) |

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
│   ├── normalizer.py      # Persian normaliser
│   ├── ids.py             # stable id generation
│   ├── tokens.py          # tiktoken token counting
│   ├── chunker.py         # semantic chunker
│   ├── renderer.py        # HTML renderer
│   ├── validator.py       # validation + quality report
│   ├── pipeline.py        # orchestrator
│   ├── cli.py             # Click CLI
│   ├── api.py             # FastAPI REST
│   └── parsers/
│       ├── base.py
│       ├── docx_parser.py
│       └── pdf_parser.py
├── tests/                 # pytest suite
├── fixtures/              # Persian contract.docx + financial_report.pdf
├── scripts/build_fixtures.py
└── examples/demo.py
```

## License

MIT.
