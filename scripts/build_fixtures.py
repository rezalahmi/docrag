"""Build the docrag fixture documents.

Produces two Persian fixtures used by the test-suite and the CLI demo:

* ``fixtures/contract.docx``  — a legal-services contract with headings,
  ordered + bulleted lists, a merged-cell pricing table and two real Word
  footnotes. Exercises the DOCX parser's numbering resolution, merge-cell grid
  and footnote paths.
* ``fixtures/financial_report.pdf`` — a Persian financial report PDF with
  section headings of distinct sizes, a financial table, a bullet list and body
  paragraphs. Built as a DOCX and converted to PDF with LibreOffice so that
  Arabic shaping + RTL are correct and text extraction returns logical order.

Run::

    python scripts/build_fixtures.py

The script also deliberately seeds normalisation hazards (Arabic Yeh/Kaf,
misplaced spaces around ``می``/``ها``, Arabic-Indic digits) so the pipeline's
normaliser is exercised on realistic input.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

FIX_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERSIAN_FONT = "DejaVu Sans"


def _set_rtl(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    pPr = paragraph._p.get_or_add_pPr()
    bidi = pPr.find(qn("w:bidi"))
    if bidi is None:
        bidi = pPr.makeelement(qn("w:bidi"), {})
        pPr.append(bidi)


def _set_run_font(paragraph, font_name: str = _PERSIAN_FONT) -> None:
    for run in paragraph.runs:
        run.font.name = font_name
        # Also set the complex-script font (cs) so Persian uses it.
        rPr = run._r.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = rPr.makeelement(qn("w:rFonts"), {})
            rPr.append(rFonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rFonts.set(qn(attr), font_name)


def _add_para(doc, text, style=None):
    p = doc.add_paragraph(text, style=style)
    _set_rtl(p)
    _set_run_font(p)
    return p


def _add_heading(doc, text, level):
    h = doc.add_heading(text, level=level)
    _set_rtl(h)
    _set_run_font(h)
    return h


# ---------------------------------------------------------------------------
# Contract DOCX
# ---------------------------------------------------------------------------

CONTRACT_FOOTNOTES = {
    1: "مبلغ قرارداد به‌صورت ناخالص و شامل مالیات بر ارزش افزوده است.",
    2: "هرگونه تأخیر در پرداخت مشمول جریمه روزانه ۰٫۵ درصد خواهد بود.",
}


def build_contract_docx(out_path: Path) -> None:
    doc = Document()
    # Set default style font so every paragraph inherits the Persian-capable font.
    normal = doc.styles["Normal"]
    normal.font.name = _PERSIAN_FONT
    normal.font.size = Pt(11)

    _add_heading(doc, "قرارداد همکاری خدمات نرم‌افزاری", level=1)

    _add_heading(doc, "مقدمه و طرفین", level=1)
    _add_para(
        doc,
        # Contains Arabic ي/ك + misplaced spaces + Arabic-Indic digit ٠ to exercise normalisation.
        "اين قرارداد در تاريخ 1403/08/01 بين شركت فناوري‌نوین پارس (كارفرما) و "
        "آقاي مهندس رضا محمدي (پيمانكار) منعقد مي‌گردد. طرفين توافق كردند كه "
        "مي بايد تمامي تعهدات را رعايت نمايند.",
    )

    _add_heading(doc, "تعاریف", level=2)
    _add_para(
        doc,
        "كارفرما: شركت درخواست‌كننده خدمات. پيمانكار: شخص حقيقي يا حقوقي اجراكننده. "
        "سيستم: نرم‌افزار موضوع قرارداد شامل كليه ماژول‌ها.",
    )

    _add_heading(doc, "دامنه خدمات", level=1)
    _add_para(doc, "پیمانکار متعهد می‌گردد خدمات زیر را ارائه نماید:")
    for item in [
        "طراحی معماری سیستم و پایگاه داده",
        "پیاده‌سازی ماژول‌های اصلی و APIها",
        "انجام تست‌های عملکردی و امنیتی",
    ]:
        _add_para(doc, item, style="List Number")
    _add_para(doc, "موارد زیر خارج از دامنه این قرارداد است:")
    for item in [
        "تأمین زیرساخت سرور و شبکه",
        "پشتیبانی پس از تحویل نهایی",
    ]:
        _add_para(doc, item, style="List Bullet")

    _add_heading(doc, "تعهدات مالی", level=1)
    _add_para(
        doc,
        # Footnote markers are inserted later by zip post-processing; we tag the
        # anchor runs with a recognisable placeholder.
        "مبلغ کل قرارداد ۴٬۵۰۰٬۰۰۰٬۰۰۰ ریال است[[FN1]] و بر اساس مراحل پیشرفت پرداخت می‌شود[[FN2]].",
    )

    # Pricing table with merged cells.
    table = doc.add_table(rows=5, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ["ردیف", "شرح خدمات", "مبلغ (ریال)", "مدت پرداخت"]
    for i, htext in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = htext
        for p in cell.paragraphs:
            _set_rtl(p)
            _set_run_font(p)
    rows_data = [
        ("۱", "طراحی و تحلیل", "۵۰۰٬۰۰۰٬۰۰۰", "پیش‌پرداخت"),
        ("۲", "پیاده‌سازی", "۲٬۵۰۰٬۰۰۰٬۰۰۰", "ماهانه"),
        ("۳", "تست و رفع اشکال", "۱٬۰۰۰٬۰۰۰٬۰۰۰", "پس از تحویل"),
        ("۴", "آموزش و مستندسازی", "۵۰۰٬۰۰۰٬۰۰۰", "پس از تحویل"),
    ]
    for r, (idx, desc, amount, term) in enumerate(rows_data, start=1):
        cells = table.rows[r].cells
        for c, val in enumerate([idx, desc, amount, term]):
            cells[c].text = val
            for p in cells[c].paragraphs:
                _set_rtl(p)
                _set_run_font(p)

    # Merges — done after filling, then the origin text is reset so python-docx's
    # merge-time text concatenation does not leave a "header\nvalue" smear.
    # Vertical: rows 3+4 share "پس از تحویل" → one cell spanning two rows.
    v_origin = table.rows[3].cells[3].merge(table.rows[4].cells[3])
    v_origin.text = "پس از تحویل"
    for p in v_origin.paragraphs:
        _set_rtl(p)
        _set_run_font(p)
    # Horizontal: row 4 شرح + مبلغ → one cell spanning two columns.
    h_origin = table.rows[4].cells[1].merge(table.rows[4].cells[2])
    h_origin.text = "آموزش و مستندسازی"
    for p in h_origin.paragraphs:
        _set_rtl(p)
        _set_run_font(p)

    _add_heading(doc, "تبصره‌ها", level=1)
    _add_para(
        doc,
        "این قرارداد در ۵ ماده و ۲ تبصره تنظیم شده و برای هر دو طرف لازم‌الاجراست. "
        "کلیه کتاب‌ها و مستندات تحویلی متعلق به کارفرما خواهد بود.",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    inject_footnotes(out_path, CONTRACT_FOOTNOTES)


# ---------------------------------------------------------------------------
# Footnote injection (zip post-processing)
# ---------------------------------------------------------------------------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_FT_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
)


def _footnotes_xml(footnotes: dict[int, str]) -> str:
    import xml.sax.saxutils as su

    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<w:footnotes xmlns:w="{_W_NS}">',
    ]
    # Separator + continuation footnotes Word always includes.
    parts.append(f'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:t> </w:t></w:r></w:p></w:footnote>')
    parts.append(f'<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:t> </w:t></w:r></w:p></w:footnote>')
    for fid, text in footnotes.items():
        esc = su.escape(text)
        parts.append(
            f'<w:footnote w:id="{fid}"><w:p>'
            f'<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:rPr><w:rStyle w:val="FootnoteText"/></w:rPr>'
            f'<w:t xml:space="preserve">{esc}</w:t></w:r></w:p></w:footnote>'
        )
    parts.append("</w:footnotes>")
    return "".join(parts)


def inject_footnotes(docx_path: Path, footnotes: dict[int, str]) -> None:
    """Add real Word footnotes to a saved .docx by rewriting the zip.

    Replaces ``[[FN<n>]]`` placeholders in document.xml with a footnote
    reference run, adds ``word/footnotes.xml``, the relationship, and the
    content-type override.
    """
    import xml.etree.ElementTree as ET

    tmp = docx_path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(docx_path, "r") as zin:
        names = zin.namelist()
        doc_xml = zin.read("word/document.xml").decode("utf-8")
        rels_xml = zin.read("word/_rels/document.xml.rels").decode("utf-8")
        try:
            ct_xml = zin.read("[Content_Types].xml").decode("utf-8")
        except KeyError:
            ct_xml = ""

        # Replace placeholders [[FN<n>]] with footnoteReference inside a run.
        for fid in footnotes:
            placeholder = f"[[FN{fid}]]"
            ref_run = (
                f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>'
                f'<w:footnoteReference w:id="{fid}"/></w:r>'
            )
            # The placeholder text is inside an existing <w:t>; split it into a
            # run with text-before, the reference, and text-after.
            doc_xml = _split_text_at_placeholder(doc_xml, placeholder, ref_run)

        # Add relationship for footnotes part if missing.
        if "footnotes.xml" not in "".join(names):
            max_id = 0
            for m in __import__("re").finditer(r'Id="rId(\d+)"', rels_xml):
                max_id = max(max_id, int(m.group(1)))
            new_id = f"rId{max_id + 1}"
            rels_root = ET.fromstring(rels_xml)
            ET.SubElement(
                rels_root, "Relationship",
                {"Id": new_id, "Type": _FT_REL_TYPE, "Target": "footnotes.xml"},
            )
            rels_xml = ET.tostring(rels_root, encoding="unicode")

        # Add content-type override for footnotes.xml.
        if "footnotes.xml" not in ct_xml:
            ct_root = ET.fromstring(ct_xml) if ct_xml else None
            if ct_root is not None:
                ET.SubElement(
                    ct_root, "Override",
                    {"PartName": "/word/footnotes.xml",
                     "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"},
                )
                ct_xml = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    + ET.tostring(ct_root, encoding="unicode")
                )

        footnotes_part = _footnotes_xml(footnotes).encode("utf-8")

        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "word/document.xml":
                    zout.writestr(item, doc_xml.encode("utf-8"))
                elif item.filename == "word/_rels/document.xml.rels":
                    zout.writestr(item, rels_xml.encode("utf-8"))
                elif item.filename == "[Content_Types].xml":
                    zout.writestr(item, ct_xml.encode("utf-8"))
                else:
                    zout.writestr(item, zin.read(item.filename))
            zout.writestr("word/footnotes.xml", footnotes_part)

    shutil.move(tmp, docx_path)


def _split_text_at_placeholder(doc_xml: str, placeholder: str, ref_run: str) -> str:
    """Replace a ``[[FNn]]`` sitting inside a ``<w:t>…</w:t>`` with split runs."""
    import re

    pattern = re.compile(
        r"(<w:t(?:\s[^>]*)?>)([^<]*?)" + re.escape(placeholder) + r"([^<]*?)(</w:t>)"
    )

    def repl(m: re.Match) -> str:
        open_tag, before, after, close_tag = m.groups()
        parts = []
        if before:
            parts.append(f"{open_tag}{before}{close_tag}")
        parts.append(ref_run)
        if after:
            parts.append(f"{open_tag}{after}{close_tag}")
        return "".join(parts)

    return pattern.sub(repl, doc_xml)


# ---------------------------------------------------------------------------
# Financial report DOCX -> PDF
# ---------------------------------------------------------------------------


def build_financial_docx(out_path: Path) -> None:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = _PERSIAN_FONT
    normal.font.size = Pt(11)

    _add_heading(doc, "گزارش مالی سال ۱۴۰۳", level=0)
    _add_para(
        doc,
        "اين گزارش عملكرد مالي شركت فناوري‌نوین پارس را براي سال مالي 1403 ارائه "
        "مي‌كند. تمامي ارقام به ريال و شامل درآمدها و هزينه‌هاي تحقق‌يافته است.",
    )

    _add_heading(doc, "۱. خلاصه مدیریتی", level=1)
    _add_para(
        doc,
        "درآمد كل شركت در سال 1403 به ۱۲ میلیارد ریال رسید که نسبت به سال قبل رشد "
        "۲۰ درصدی را نشان مي‌دهد. هزينه‌های عملیاتی نیز با كنترل مناسب، رشد كمتری داشت.",
    )

    _add_heading(doc, "۲. عملکرد سالانه", level=1)
    # A financial table mirroring the user's example.
    table = doc.add_table(rows=4, cols=4)
    table.style = "Table Grid"
    headers = ["سال", "درآمد", "هزینه", "سود"]
    for i, htext in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = htext
        for p in cell.paragraphs:
            _set_rtl(p)
            _set_run_font(p)
    data = [
        ("۱۴۰۱", "۸٬۰۰۰٬۰۰۰٬۰۰۰", "۵٬۰۰۰٬۰۰۰٬۰۰۰", "۳٬۰۰۰٬۰۰۰٬۰۰۰"),
        ("۱۴۰۲", "۱۰٬۰۰۰٬۰۰۰٬۰۰۰", "۶٬۰۰۰٬۰۰۰٬۰۰۰", "۴٬۰۰۰٬۰۰۰٬۰۰۰"),
        ("۱۴۰۳", "۱۲٬۰۰۰٬۰۰۰٬۰۰۰", "۶٬۵۰۰٬۰۰۰٬۰۰۰", "۵٬۵۰۰٬۰۰۰٬۰۰۰"),
    ]
    for r, row in enumerate(data, start=1):
        for c, val in enumerate(row):
            cell = table.rows[r].cells[c]
            cell.text = val
            for p in cell.paragraphs:
                _set_rtl(p)
                _set_run_font(p)

    _add_heading(doc, "۳. نکات کلیدی", level=1)
    _add_para(doc, "نکات برجسته عملکرد سال ۱۴۰۳ به شرح زیر است:")
    for item in [
        "رشد ۲۰ درصدی درآمد نسبت به سال قبل",
        "کاهش نسبت هزینه به درآمد از ۶۰ به ۵۴ درصد",
        "افزایش حاشیه سود به ۴۶ درصد",
    ]:
        _add_para(doc, item, style="List Bullet")

    _add_heading(doc, "۴. ترازنامه و صورت سود و زیان", level=1)
    _add_para(
        doc,
        "ترازنامه شرکت در پایان سال ۱۴۰۳ نشان‌دهنده افزایش دارایی‌های جاری و "
        "کاهش بدهی‌های کوتاه‌مدت است. صورت سود و زیان نیز تأیید می‌کند که سود خالص "
        "۵٬۵۰۰٬۰۰۰٬۰۰۰ ریال محقق شده است. می‌توان نتیجه گرفت مدیریت مالی کارآمد بوده است.",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)


def convert_to_pdf(docx_path: Path, out_pdf: Path) -> None:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf",
         "--outdir", str(out_pdf.parent), str(docx_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    produced = out_pdf.parent / (docx_path.stem + ".pdf")
    if produced != out_pdf:
        shutil.move(produced, out_pdf)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    contract = FIX_DIR / "contract.docx"
    fin_docx = FIX_DIR / "_financial_report.docx"
    fin_pdf = FIX_DIR / "financial_report.pdf"

    print("Building contract.docx ...")
    build_contract_docx(contract)
    print("  ->", contract)

    print("Building financial_report.pdf ...")
    build_financial_docx(fin_docx)
    convert_to_pdf(fin_docx, fin_pdf)
    fin_docx.unlink(missing_ok=True)  # keep only the PDF as the fixture
    print("  ->", fin_pdf)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
