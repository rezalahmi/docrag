"""Tests for id generation."""
from docrag.ids import chunk_id, make_id, slugify


def test_slugify_keeps_persian():
    assert slugify("قرارداد۱۴۰۳") == "قرارداد۱۴۰۳"


def test_slugify_replaces_unsafe():
    assert slugify("a/b:c?d") == "a_b_c_d"


def test_slugify_replaces_space():
    assert slugify("قرارداد ۱۴۰۳") == "قرارداد_۱۴۰۳"


def test_slugify_truncates():
    assert len(slugify("x" * 100, max_len=20)) == 20


def test_make_id_joins_parts():
    assert make_id("contract.docx", "p5", "table", 2) == "contract.docx::p5::table::2"


def test_chunk_id_format():
    assert chunk_id("contract", 7) == "contract::chunk::0007"
