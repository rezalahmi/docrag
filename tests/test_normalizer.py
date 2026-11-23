"""Tests for the Persian normaliser."""
from __future__ import annotations

from docrag.normalizer import Normalizer, NormalizerConfig, normalize


def test_arabic_yeh_kaf_to_persian():
    assert normalize("كيك يدخل") == "کیک یدخل"


def test_alef_madda_normalized():
    # آ → ا by default (safe-for-search folding).
    assert normalize("آمد") == "امد"


def test_ta_marbuta_to_heh():
    assert normalize("مدرسة") == "مدرسه"


def test_prefix_zwnj_joining():
    assert normalize("مي كند") == "می\u200cکند"


def test_already_zwnj_preserved():
    assert normalize("نمی‌خواهم") == "نمی\u200cخواهم"


def test_suffix_zwnj_joining():
    assert normalize("كتاب ها") == "کتاب\u200cها"


def test_arabic_indic_digits_to_persian():
    assert normalize("٠١٢٣") == "۰۱۲۳"


def test_latin_digits_default_to_persian():
    assert normalize("سال 1403") == "سال ۱۴۰۳"


def test_digits_latin_mode():
    out = normalize("سال 1403 و ١٤", NormalizerConfig(digits="latin"))
    assert "1403" in out and "14" in out
    assert "۱" not in out


def test_digits_keep_mode_keeps_latin():
    out = normalize("سال 1403 و ١٤", NormalizerConfig(digits="keep"))
    # Arabic-indic still folded to Persian; Latin left alone.
    assert "1403" in out
    assert "١" not in out  # arabic-indic folded
    assert "۱۴" in out     # arabic-indic ١٤ -> ۱۴


def test_double_zwnj_collapsed():
    assert normalize("سلام‌‌‌جان") == "سلام\u200cجان"


def test_leading_trailing_zwnj_removed():
    assert normalize("‌سلام‌") == "سلام"


def test_punct_spacing_fixed():
    assert normalize("كلمه ،  فاصله") == "کلمه، فاصله"


def test_paren_inner_padding_stripped():
    assert normalize("( پرانتز )") == "(پرانتز)"


def test_paren_outer_spacing_preserved():
    assert normalize("متن (داخلی) بیرون") == "متن (داخلی) بیرون"


def test_dir_controls_removed():
    assert normalize("a\u200eb\u200fc") == "abc"


def test_curly_quotes_straightened():
    assert normalize("“تست” و ‘تست’") == '"تست" و \'تست\''


def test_tatweel_removed():
    assert normalize("عــلي") == "علی"


def test_empty_and_none():
    assert normalize("") == ""
    assert normalize(None) == ""  # type: ignore[arg-type]


def test_idempotent():
    once = normalize("مي‌كند و كتاب ها")
    twice = normalize(once)
    assert once == twice


def test_normalizer_instance_callable():
    n = Normalizer()
    assert n("مي كند") == "می\u200cکند"


def test_invalid_digits_raises():
    import pytest
    with pytest.raises(ValueError):
        NormalizerConfig(digits="bogus").validate()
