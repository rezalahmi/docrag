"""Tests for DOCX numbering resolution (multi-level markers + restart)."""
from __future__ import annotations

from docrag.normalizer import Normalizer
from docrag.parsers.docx_parser import NumberingResolver

# A minimal numbering.xml: one abstractNum with two decimal levels whose
# lvlText builds "1." (level 0) and "1.1." (level 1).
_NUMBERING_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1.%2."/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1">
    <w:abstractNumId w:val="0"/>
  </w:num>
</w:numbering>
"""


def _resolver() -> NumberingResolver:
    return NumberingResolver.from_xml(_NUMBERING_XML.encode("utf-8"))


def test_unknown_numbering_returns_empty():
    r = NumberingResolver.from_xml(None)
    m, ordered = r.marker(99, 0, Normalizer())
    assert m == "" and ordered is False


def test_sequential_decimal_markers():
    r = _resolver()
    n = Normalizer()
    m1, o1 = r.marker(1, 0, n)
    m2, o2 = r.marker(1, 0, n)
    m3, o3 = r.marker(1, 0, n)
    assert (m1, m2, m3) == ("۱.", "۲.", "۳.")
    assert (o1, o2, o3) == (True, True, True)


def test_nested_markers_and_restart():
    r = _resolver()
    n = Normalizer()
    # 1. / 2. / 2.1. / 2.2. / 3.
    seq = [
        (1, 0), (1, 0),   # 1. 2.
        (1, 1), (1, 1),   # 2.1. 2.2.
        (1, 0),           # 3.
    ]
    out = [r.marker(num_id, ilvl, n)[0] for num_id, ilvl in seq]
    assert out == ["۱.", "۲.", "۲.۱.", "۲.۲.", "۳."]


def test_bullet_level_not_ordered():
    xml = _NUMBERING_XML.replace(
        '<w:numFmt w:val="decimal"/>', '<w:numFmt w:val="bullet"/>', 1
    )
    r = NumberingResolver.from_xml(xml.encode("utf-8"))
    m, ordered = r.marker(1, 0, Normalizer())
    assert ordered is False
