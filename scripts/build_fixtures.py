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

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import openpyxl
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

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


def _soffice_cmd() -> str | None:
    """Resolve the LibreOffice executable across platforms.

    On Linux/macOS it is usually on PATH as ``soffice`` or ``libreoffice``.
    On Windows the installer rarely adds it to PATH, so the standard
    ``Program Files\\LibreOffice\\program\\soffice.exe`` locations are probed
    too. Returns ``None`` when LibreOffice cannot be found.
    """
    for name in ("soffice", "soffice.exe", "libreoffice", "libreoffice.exe"):
        path = shutil.which(name)
        if path:
            return path
    for base in (
        r"C:\Program Files\LibreOffice\program",
        r"C:\Program Files (x86)\LibreOffice\program",
    ):
        exe = Path(base) / "soffice.exe"
        if exe.exists():
            return str(exe)
    return None


def _soffice_missing_msg() -> str:
    return (
        "LibreOffice (soffice) not found. It is needed only to build the PDF "
        "fixture (DOCX→PDF) and to recalc the XLSX formulas. Install "
        "LibreOffice or add its 'program' directory to PATH. The other "
        "fixtures and most of the test-suite do not need it; the tests that "
        "depend on the PDF / formula-recalc'd XLSX will skip."
    )


def convert_to_pdf(docx_path: Path, out_pdf: Path) -> None:
    soffice = _soffice_cmd()
    if soffice is None:
        raise RuntimeError(_soffice_missing_msg())
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf",
         "--outdir", str(out_pdf.parent), str(docx_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    produced = out_pdf.parent / (docx_path.stem + ".pdf")
    if produced != out_pdf:
        shutil.move(produced, out_pdf)


# ---------------------------------------------------------------------------
# XLSX fixtures (agencies + financial with formulas)
# ---------------------------------------------------------------------------
_XLSX_HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
_XLSX_HEADER_FONT = Font(bold=True)
_XLSX_TITLE_FONT = Font(bold=True, size=13)
_XLSX_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_XLSX_NUM_FMT = "#,##0"


def _style_xlsx_header(ws, rng):
    for row in ws[rng]:
        for c in row:
            c.font = _XLSX_HEADER_FONT
            c.fill = _XLSX_HEADER_FILL
            c.alignment = _XLSX_CENTER


def build_agencies_xlsx(out_path: Path) -> None:
    """نمایندگی‌ها.xlsx — single-level header, merged 'منطقه' column spanning
    row pairs (tests merged-cell propagation), phone+code as text, one hidden row,
    plus a second sheet so multi-sheet parsing is exercised."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "اطلاعات نمایندگی‌ها"
    ws.merge_cells("A1:F1")
    ws["A1"] = "فهرست نمایندگی‌های شرکت"
    ws["A1"].font = _XLSX_TITLE_FONT
    ws["A1"].alignment = _XLSX_CENTER
    headers = ["منطقه", "کد", "نام نمایندگی", "شهر", "تلفن", "آدرس"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=2, column=i, value=h)
    _style_xlsx_header(ws, "A2:F2")
    rows = [
        ("شمال", "101", "نمایندگی البرز", "کرج", "02632550000", "کرج، خیابان فردوسی، پلاک ۱۲۰"),
        ("شمال", "102", "نمایندگی رشت", "رشت", "01333445566", "رشت، میدان شهدا، ساختمان آرا"),
        ("جنوب", "103", "نمایندگی اصفهان", "اصفهان", "03134567890", "اصفهان، خیابان چهارباغ بالا، پلاک ۵۵"),
        ("جنوب", "104", "نمایندگی شیراز", "شیراز", "07132345678", "شیراز، خیابان زند، روبروی شهرداری"),
    ]
    for ri, row in enumerate(rows, start=3):
        for ci, val in enumerate(row, start=1):
            cell = ws.cell(row=ri, column=ci, value=val)
            if ci in (2, 5):  # کد and تلفن → force text
                cell.number_format = "@"
    ws.merge_cells("A3:A4")
    ws.merge_cells("A5:A6")
    ws.row_dimensions[6].hidden = True  # hide شیراز row → tests hidden-row tracking
    for col, w in zip("ABCDEF", (10, 8, 22, 12, 16, 38)):
        ws.column_dimensions[col].width = w

    # Second sheet — head-office contact.
    ws2 = wb.create_sheet("اطلاعات تماس مرکزی")
    ws2.merge_cells("A1:C1")
    ws2["A1"] = "اطلاعات تماس دفتر مرکزی"
    ws2["A1"].font = _XLSX_TITLE_FONT
    ws2["A1"].alignment = _XLSX_CENTER
    for i, h in enumerate(["نام واحد", "تلفن", "ایمیل"], start=1):
        ws2.cell(row=2, column=i, value=h)
    _style_xlsx_header(ws2, "A2:C2")
    ws2.cell(row=3, column=1, value="دفتر مرکزی")
    ws2.cell(row=3, column=2, value="02188770000").number_format = "@"
    ws2.cell(row=3, column=3, value="info@example.ir")
    for col, w in zip("ABC", (18, 16, 24)):
        ws2.column_dimensions[col].width = w

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    wb.close()


def build_financial_xlsx(out_path: Path) -> None:
    """صورت_مالی.xlsx — two-level header (سال rowspan, درآمد/هزینه colspan with
    کل/عملیاتی children, سود rowspan, نرخ رشد rowspan), three year rows.
    سود is a FORMULA (=B-D) recalculated via LibreOffice so a cached value is
    stored; نرخ رشد is a formula whose cached value is then stripped so the
    'needs_review' path is exercised."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "صورت سود و زیان"
    ws.sheet_view.rightToLeft = True
    ws.merge_cells("A1:G1")
    ws["A1"] = "صورت سود و زیان سال‌های ۱۴۰۰ تا ۱۴۰۲ (به ریال)"
    ws["A1"].font = _XLSX_TITLE_FONT
    ws["A1"].alignment = _XLSX_CENTER
    ws.merge_cells("A2:A3"); ws["A2"] = "سال"
    ws.merge_cells("B2:C2"); ws["B2"] = "درآمد"
    ws.merge_cells("D2:E2"); ws["D2"] = "هزینه"
    ws.merge_cells("F2:F3"); ws["F2"] = "سود"
    ws.merge_cells("G2:G3"); ws["G2"] = "نرخ رشد"
    ws["B3"] = "کل"; ws["C3"] = "عملیاتی"
    ws["D3"] = "کل"; ws["E3"] = "عملیاتی"
    _style_xlsx_header(ws, "A2:G3")
    data = [
        (1400, 10_000_000_000, 9_500_000_000, 6_000_000_000, 5_800_000_000, None),
        (1401, 12_000_000_000, 11_000_000_000, 7_000_000_000, 6_700_000_000, 10_000_000_000),
        (1402, 15_000_000_000, 14_000_000_000, 8_000_000_000, 7_700_000_000, 12_000_000_000),
    ]
    for i, (yr, inc_t, inc_o, cost_t, cost_o, _) in enumerate(data):
        r = 4 + i
        ws.cell(row=r, column=1, value=yr).number_format = "0"
        ws.cell(row=r, column=2, value=inc_t).number_format = _XLSX_NUM_FMT
        ws.cell(row=r, column=3, value=inc_o).number_format = _XLSX_NUM_FMT
        ws.cell(row=r, column=4, value=cost_t).number_format = _XLSX_NUM_FMT
        ws.cell(row=r, column=5, value=cost_o).number_format = _XLSX_NUM_FMT
        ws.cell(row=r, column=6, value=f"=B{r}-D{r}").number_format = _XLSX_NUM_FMT
        if i > 0:
            ws.cell(row=r, column=7, value=f"=B{r}/B{r-1}-1").number_format = "0.00%"
        else:
            ws.cell(row=r, column=7, value="—")
    for col, w in zip("ABCDEFG", (8, 18, 18, 18, 18, 18, 12)):
        ws.column_dimensions[col].width = w

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    wb.close()
    # The سود formula needs LibreOffice to compute and cache its result. If
    # soffice is unavailable, remove the file so the financial tests SKIP
    # rather than fail on an un-recalc'd formula (سود would read as
    # "نیازمند بررسی" instead of the expected value).
    try:
        _recalc_xlsx(out_path)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise
    _strip_cached_values(out_path, ["G5", "G6"])  # نرخ رشد → needs_review


def _recalc_xlsx(path: Path) -> None:
    soffice = _soffice_cmd()
    if soffice is None:
        raise RuntimeError(_soffice_missing_msg())
    tmp = Path(tempfile.mkdtemp())
    try:
        subprocess.run(
            [soffice, "--headless", "--calc", "--convert-to", "xlsx",
             "--outdir", str(tmp), str(path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120,
        )
        converted = tmp / path.name
        if converted.exists():
            shutil.move(str(converted), str(path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _strip_cached_values(path: Path, cells: list[str]) -> None:
    tmp = str(path) + ".tmp"
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet") and item.filename.endswith(".xml"):
                text = data.decode("utf-8")
                for addr in cells:
                    text = _strip_v_in_cell(text, addr)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    shutil.move(tmp, str(path))


def _strip_v_in_cell(xml: str, addr: str) -> str:
    pat = re.compile(r'(<c r="%s"[^>]*>)(.*?)(</c>)' % re.escape(addr))
    def repl(m):
        inner = re.sub(r"<v>[^<]*</v>", "", m.group(2))
        return m.group(1) + inner + m.group(3)
    return pat.sub(repl, xml)


# ---------------------------------------------------------------------------
# Sample HTML page (URL fixture)
# ---------------------------------------------------------------------------
SAMPLE_HTML = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>درباره شرکت فناوری‌نوین پارس</title>
<meta name="description" content="شرکت فناوری‌نوین پارس ارائه‌دهنده خدمات نرم‌افزاری و راهکارهای ابری برای کسب‌وکارهای ایرانی.">
</head>
<body>
<nav><a href="/">خانه</a> <a href="/products/">محصولات</a> <a href="/blog/">وبلاگ</a></nav>
<header><h1>درباره شرکت فناوری‌نوین پارس</h1>
<p>شرکت فناوری‌نوین پارس از سال ۱۳۹۵ فعالیت خود را در حوزه توسعه نرم‌افزار آغاز کرده است.</p></header>

<main>
<section>
<h2>خدمات ما</h2>
<p>ما خدمات زیر را به سازمان‌های دولتی و خصوصی ارائه می‌دهیم. تمام راهکارها به‌صورت بومی‌سازی‌شده و مطابق با الزامات قانونی کشور هستند.</p>
<table>
<thead><tr><th>نام خدمت</th><th>شرح</th><th>حداقل قیمت (ریال)</th></tr></thead>
<tbody>
<tr><td>طراحی نرم‌افزار اختصاصی</td><td>تحلیل، طراحی و پیاده‌سازی نرم‌افزار سفارشی</td><td>۵٬۰۰۰٬۰۰۰٬۰۰۰</td></tr>
<tr><td>راهکار ابری</td><td>میزبانی و نگهداری زیرساخت ابری</td><td>۲٬۰۰۰٬۰۰۰٬۰۰۰</td></tr>
<tr><td>پشتیبانی فنی</td><td>پشتیبانی ۲۴ ساعته و رفع اشکال</td><td>۱٬۰۰۰٬۰۰۰٬۰۰۰</td></tr>
</tbody>
</table>
</section>

<section>
<h2>مزایای رقابتی</h2>
<ul>
<li>تیم متخصص با بیش از ۵۰ مهندس</li>
<li>پشتیبانی فارسی‌زبان در تمام مراحل</li>
<li>تحویل به‌موقع و تعهد به کیفیت</li>
</ul>
</section>

<section>
<h2>تماس با ما</h2>
<p>برای کسب اطلاعات بیشتر با شماره ۰۲۱-۸۸۷۷۰۰۰۰ تماس بگیرید یا به ایمیل info@example.ir پیام بفرستید. دفتر مرکزی در تهران، خیابان ولیعصر واقع شده است.</p>
</section>
</main>

<footer>
<!-- WordPress/Elementor duplicate: mobile + desktop footer rendered together. -->
<div class="footer-desktop">© ۱۴۰۳ فناوری‌نوین پارس. تمام حقوق محفوظ است.</div>
<div class="footer-mobile">© ۱۴۰۳ فناوری‌نوین پارس. تمام حقوق محفوظ است.</div>
</footer>
</body>
</html>
"""


def build_sample_html(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(SAMPLE_HTML, encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    contract = FIX_DIR / "contract.docx"
    fin_docx = FIX_DIR / "_financial_report.docx"
    fin_pdf = FIX_DIR / "financial_report.pdf"
    agencies = FIX_DIR / "نمایندگی‌ها.xlsx"
    financial_xlsx = FIX_DIR / "صورت_مالی.xlsx"
    sample_html = FIX_DIR / "sample_page.html"

    steps = [
        ("contract.docx", lambda: build_contract_docx(contract)),
        ("financial_report.pdf", lambda: _build_financial_pdf(fin_docx, fin_pdf)),
        ("نمایندگی‌ها.xlsx", lambda: build_agencies_xlsx(agencies)),
        ("صورت_مالی.xlsx", lambda: build_financial_xlsx(financial_xlsx)),
        ("sample_page.html", lambda: build_sample_html(sample_html)),
    ]
    failures: list[str] = []
    for label, fn in steps:
        print(f"Building {label} ...")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — best-effort fixture build
            print(f"  !! skipped {label}: {e}")
            failures.append(label)
            continue
        target = {
            "contract.docx": contract, "financial_report.pdf": fin_pdf,
            "نمایندگی‌ها.xlsx": agencies, "صورت_مالی.xlsx": financial_xlsx,
            "sample_page.html": sample_html,
        }[label]
        if target.exists():
            print("  ->", target)
    if failures:
        print(f"\nDone with {len(failures)} skipped: {', '.join(failures)}")
        print("(Tests depending on the skipped fixtures will be skipped, not failed.)")
    else:
        print("\nDone.")
    return 0


def _build_financial_pdf(fin_docx: Path, fin_pdf: Path) -> None:
    build_financial_docx(fin_docx)
    convert_to_pdf(fin_docx, fin_pdf)
    fin_docx.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
