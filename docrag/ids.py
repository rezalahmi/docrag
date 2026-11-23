"""Stable, human-readable identifier generation for IR elements and chunks.

Identifiers are deterministic given their inputs so that re-processing the same
file yields the same ids (useful for diffing and caching). They are also
self-describing: an id such as ``contract.pdf::p5::table::2`` immediately tells
a reader which file, page, type and ordinal an element came from.
"""

from __future__ import annotations

import re
from typing import Iterable

_SAFE = re.compile(r"[^A-Za-z0-9\u0600-\u06FF\u200C._\-]+")

_ID_SEP = "::"


def slugify(value: str, *, max_len: int = 40) -> str:
    """Collapse ``value`` into a filesystem/JSON-safe slug.

    Persian letters, digits, ZWNJ, ASCII alphanumerics, ``.``, ``_`` and ``-``
    are kept; everything else becomes ``_``. The result is trimmed and
    truncated so ids never blow up downstream stores.
    """
    if value is None:
        return ""
    slug = _SAFE.sub("_", str(value)).strip("._-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("._-")
    return slug or "x"


def make_id(*parts: str | int) -> str:
    """Join ``parts`` into a ``::``-separated, slugified identifier."""
    cleaned = [slugify(str(p)) for p in parts if str(p) != ""]
    return _ID_SEP.join(cleaned)


def chunk_id(prefix: str, ordinal: int) -> str:
    """Build a chunk id like ``contract::chunk::0007``."""
    return f"{slugify(prefix)}{_ID_SEP}chunk{_ID_SEP}{ordinal:04d}"


def join_ids(ids: Iterable[str]) -> str:
    """Join several ids with a separator for display purposes."""
    return ", ".join(ids)
