# Fixtures

Two Persian documents used by the test-suite and the demo. They are **inputs**,
not deliverables — they exercise the parser on realistic content (headings,
lists, merged-cell tables, footnotes, financial tables, RTL).

| File | Type | What it tests |
|---|---|---|
| `contract.docx` | DOCX | Headings (multi-level), ordered + bulleted lists with real Word numbering, a pricing table with **vertical and horizontal merged cells**, two real footnotes, and seeded normalisation hazards (Arabic ي/ك, misplaced spaces around می/ها, Arabic-Indic digits). |
| `financial_report.pdf` | PDF | Section headings of distinct font sizes, a financial table (سال/درآمد/هزینه/سود), a bullet list, body paragraphs. Built as a DOCX and converted to PDF with LibreOffice so Arabic shaping + RTL are correct and text extraction returns logical order. |

Regenerate them anytime:

```bash
python scripts/build_fixtures.py
```

The contract DOCX is generated with `python-docx`; footnotes are injected by
post-processing the .docx zip (`word/footnotes.xml` + relationship + content
type), because python-docx has no footnote API. The financial PDF is a DOCX
converted via `soffice --headless --convert-to pdf` (requires LibreOffice).
