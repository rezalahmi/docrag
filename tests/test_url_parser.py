"""URL / HTML parser → IR tests."""
from __future__ import annotations

from docrag import ElementKind, UrlParser, get_parser


def test_get_parser_dispatches_html(sample_html):
    assert isinstance(get_parser(sample_html), UrlParser)


def test_get_parser_dispatches_url_scheme():
    assert isinstance(get_parser("https://example.com/page"), UrlParser)


def test_url_parser_produces_document(sample_html):
    from docrag import Pipeline
    doc = Pipeline().run(sample_html).document
    assert doc.file_type == "url"
    # Front-matter metadata carried through.
    assert doc.metadata.get("title") == "درباره شرکت فناوری‌نوین پارس"
    assert doc.metadata.get("source_url") == str(sample_html)
    assert doc.metadata.get("method") == "static HTML"


def test_headings_and_sections(sample_html):
    from docrag import Pipeline
    doc = Pipeline().run(sample_html).document
    headings = [e.text for e in doc if e.kind == ElementKind.HEADING]
    assert "درباره شرکت فناوری‌نوین پارس" in headings
    assert "خدمات ما" in headings
    assert "مزایای رقابتی" in headings
    assert "تماس با ما" in headings


def test_html_table_becomes_ir_table(sample_html):
    from docrag import Pipeline
    doc = Pipeline().run(sample_html).document
    tables = list(doc.tables())
    assert len(tables) == 1
    tbl = tables[0]
    # Header row carries the three column names.
    header_texts = [c.text for c in tbl.rows[0].cells]
    assert "نام خدمت" in header_texts
    assert "حداقل قیمت (ریال)" in header_texts
    # First data row is the custom-software service.
    flat = "\n".join(c.text for r in tbl.rows for c in r.cells)
    assert "طراحی نرم‌افزار اختصاصی" in flat
    assert "۵٬۰۰۰٬۰۰۰٬۰۰۰" in flat


def test_html_list_becomes_list_items(sample_html):
    from docrag import Pipeline
    doc = Pipeline().run(sample_html).document
    items = [e for e in doc if e.kind == ElementKind.LIST_ITEM]
    assert len(items) >= 3
    texts = " ".join(e.text for e in items)
    assert "تیم متخصص" in texts
    assert "پشتیبانی فارسی‌زبان" in texts


def test_duplicate_mobile_desktop_footer_deduped(sample_html):
    """The two identical footer divs (mobile/desktop) collapse to one."""
    from docrag import Pipeline
    doc = Pipeline().run(sample_html).document
    all_text = " ".join(e.text for e in doc)
    # The copyright line should appear at most once across all elements.
    assert all_text.count("تمام حقوق محفوظ است") == 1


def test_url_chunks_carry_source(sample_html):
    from docrag import Pipeline
    chunks = Pipeline().run(sample_html).chunks
    assert chunks
    # Every chunk's source points back to the ingested file/URL.
    for c in chunks:
        assert c.source is not None
        assert c.source.file == str(sample_html)


def test_url_parser_accepts_html_string_via_fetcher():
    """A custom fetcher lets the parser ingest HTML without network access."""
    html = (
        "<html lang='fa' dir='rtl'><head><title>تست</title>"
        "<meta name='description' content='صفحه تست'></head>"
        "<body><h1>عنوان</h1><p>پاراگراف نمونه.</p></body></html>"
    )
    parser = UrlParser(fetcher=lambda url, timeout, ua: (html, url))
    doc = parser.parse("https://example.test/fa", doc_id="test")
    assert doc.metadata["title"] == "تست"
    assert any(e.text == "عنوان" for e in doc if e.kind == ElementKind.HEADING)
    assert any("پاراگراف نمونه" in e.text for e in doc)
