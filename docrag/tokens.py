"""Token counting for chunk budgeting.

Uses OpenAI's ``cl100k_base`` encoding (the family used by GPT-4 / GPT-3.5
embeddings) so chunk sizes match what a typical retrieval stack actually sees.
Persian text is multi-byte UTF-8, so a 400-token budget is *not* 400 words;
counting tokens rather than characters is what keeps a chunk within an
embedding model's window.

The encoder is loaded lazily and cached so importing the package never pays for
it unless chunking is actually used.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - fall back when tiktoken has no data
        return None


def count_tokens(text: str) -> int:
    """Return the cl100k_base token count of ``text``.

    Falls back to a conservative ``len(text) / 3`` estimate (Persian averages
    ~3 chars/token) if the encoder is unavailable, so the pipeline still works
    in restricted environments.
    """
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        return max(1, len(text) // 3)
    return len(enc.encode(text))


def estimate_tokens(text: Optional[str]) -> int:
    """Alias used by callers that may pass ``None``."""
    return count_tokens(text or "")
