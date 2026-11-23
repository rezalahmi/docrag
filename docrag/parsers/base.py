"""Common base class for all parsers."""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any

from ..ir import Document
from ..normalizer import Normalizer, NormalizerConfig


class BaseParser(abc.ABC):
    """Interface every parser implements.

    A parser is constructed with an optional :class:`Normalizer` (so the same
    normalisation config is shared across the pipeline) and produces a
    :class:`Document` from a path.
    """

    file_type: str = "unknown"

    def __init__(self, normalizer: Normalizer | None = None) -> None:
        self.normalizer = normalizer or Normalizer()

    def normalize(self, text: str) -> str:
        return self.normalizer.normalize(text)

    @abc.abstractmethod
    def parse(self, path: str | Path, *, doc_id: str | None = None) -> Document:
        """Parse ``path`` and return a :class:`Document`."""

    def make_metadata(self, path: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "file_name": path.name,
            "file_type": self.file_type,
            "file_size": path.stat().st_size if path.exists() else 0,
        }
        if extra:
            meta.update(extra)
        return meta
