"""Persian text normaliser.

A normaliser is not cosmetic for Persian RAG: without it the same word spelled
with Arabic Yeh (``ي``) and Persian Yeh (``ی``) becomes two distinct tokens, a
``می‌کند`` split by a plain space instead of ZWNJ does not match ``می‌کند``
written correctly, and Arabic-Indic digits (``٠``) hide from queries written in
Persian digits (``۰``). Retrieval precision collapses.

This module consolidates the fixes that matter for retrieval into one
configurable :class:`Normalizer`:

1. **Letter canonicalisation** - Arabic ``ي``/``ك``/``ى``/``ة`` → Persian
   counterparts; removal of tatweel and zero-width marks left by editors.
2. **ZWNJ spacing** - ``می کند`` → ``می‌کند`` and ``کتاب ها`` → ``کتاب‌ها``;
   stray ZWNJ at word boundaries is trimmed.
3. **Space cleanup** - collapse runs of spaces, fix spacing around punctuation
   and parentheses, remove the exotic spaces (NBSP, NNBSP, thin space, …).
4. **Digits** - normalise Arabic-Indic → Persian (default), with options to also
   fold Latin digits or leave them untouched.
5. **Direction marks** - strip LRE/RLE/PDF/LRM/RLM. They are layout controls,
   not content, and leak into embeddings as noise.
6. **Quotes & punctuation** - fold curly quotes and guillemets to a consistent
   form (Persian guillemets ``«»`` are kept, ASCII quotes are straightened).

The default :func:`normalize` is tuned for retrieval: it does *not* lowercase
(Persian has no case) and keeps Latin letters/digits intact unless told to fold
digits, because identifiers, currency codes and dates in source documents are
usually Latin.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Character classes
# --------------------------------------------------------------------------- #

ZWNJ = "\u200c"  # ZERO WIDTH NON-JOINER
ZWJ = "\u200d"   # ZERO WIDTH JOINER
ZWS = "\u200b"   # ZERO WIDTH SPACE

# Bidirectional / formatting control characters that should not survive into
# chunks. Removed unconditionally (they are never content).
_DIR_CONTROLS = {
    "\u200e",  # LRM
    "\u200f",  # RLM
    "\u202a",  # LRE
    "\u202b",  # RLE
    "\u202c",  # PDF
    "\u202d",  # LRO
    "\u202e",  # RLO
    "\u2066",  # LRI
    "\u2067",  # RLI
    "\u2068",  # FSI
    "\u2069",  # PDI
    "\ufeff",  # BOM / ZWNBSP
}

# Exotic spaces collapsed into a normal space, then deduplicated.
_EXOTIC_SPACES = {
    "\u00a0",  # NBSP
    "\u202f",  # NNBSP
    "\u2007",  # FIGURE SPACE
    "\u2008",  # PUNCTUATION SPACE
    "\u2009",  # THIN SPACE
    "\u200a",  # HAIR SPACE
    "\u200b",  # ZERO WIDTH SPACE
    "\u205f",  # MEDIUM MATHEMATICAL SPACE
    "\u3000",  # IDEOGRAPHIC SPACE
    "\t",      # tab → space
}

# Letter substitutions: Arabic forms commonly mistyped in Persian text.
_LETTER_MAP = {
    "\u064a": "\u06cc",  # Arabic Yeh ي → Persian Yeh ی
    "\u0649": "\u06cc",  # Alef Maksura ى → Persian Yeh ی
    "\u0643": "\u06a9",  # Arabic Kaf ك → Persian Kaf ک
    "\u0625": "\u0627",  # Alef with Hamza below إ → Alef ا
    "\u0623": "\u0627",  # Alef with Hamza above أ → Alef ا
    "\u0622": "\u0627",  # Alef Madda آ → Alef ا  (debatable; safe for search)
    "\u0629": "\u0647",  # Ta Marbuta ة → Heh ه
    "\u0640": "",        # Arabic Tatweel ـ removed
}

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_LATIN_DIGITS = "0123456789"

_ARABIC_TO_PERSIAN = str.maketrans(_ARABIC_INDIC_DIGITS, _PERSIAN_DIGITS)
_LATIN_TO_PERSIAN = str.maketrans(_LATIN_DIGITS, _PERSIAN_DIGITS)
_PERSIAN_TO_LATIN = str.maketrans(_PERSIAN_DIGITS, _LATIN_DIGITS)

# Curly / fullwidth quotes → ASCII straight quotes.
_QUOTE_MAP = {
    "\u2018": "'",  # ‘
    "\u2019": "'",  # ’
    "\u201c": '"',  # “
    "\u201d": '"',  # ”
    "\u2032": "'",  # ′
    "\u2033": '"',  # ″
}

# --------------------------------------------------------------------------- #
# ZWNJ affix rules
# --------------------------------------------------------------------------- #
# Prefixes that must attach to the following word with a ZWNJ. These are the
# high-frequency verb prefixes and a few bound morphemes. Compiled at import so
# the regex is built once.
_PREFIXES = [
    "می", "نمی", "نمی‌",  # present/conditional negation
    "بی", "نا", "بد",     # privative/negative
    "پیشا", "پسا", "فوق",
    "هم", "نا",
]

# Suffixes attached to the preceding word with a ZWNJ.
_SUFFIXES = [
    "ها", "های", "هایی", "هایت", "هایش", "هایمان", "هایتان", "هایشان",  # plural
    "ات", "ان", "انی",
    "اش", "ام", "ایم", "اید", "اند",     # enclitic pronouns
    "تر", "تری",                          # comparative
    "ترین",                               # superlative
    "گر", "گری", "گری‌ها",
    "شناس", "شناسی",
    "شان", "مان", "تان",
]

# Build alternations longest-first so "های" wins over "ها" when both match.
_PREFIXES.sort(key=len, reverse=True)
_SUFFIXES.sort(key=len, reverse=True)

# "می کند" → "می‌کند" (prefix + space + word). We require the space to be a
# real space (not ZWNJ) so we don't touch already-correct forms.
_PREFIX_SPACE_RE = re.compile(
    r"(?<![\u0600-\u06FF\u200c])(" + "|".join(_PREFIXES) + r") +(?=[\u0600-\u06FF])"
)
# "کتاب ها" → "کتاب‌ها" (word + space + suffix, suffix at word end).
_SUFFIX_SPACE_RE = re.compile(
    r"(?<=[\u0600-\u06FF]) +(" + "|".join(_SUFFIXES) + r")(?![\u0600-\u06FF])"
)

# Stray ZWNJ at the very start/end of a word or doubled: cleaned up.
_LEADING_ZWNJ = re.compile(r"(?<![\u0600-\u06FF])\u200c+")
_TRAILING_ZWNJ = re.compile(r"\u200c+(?![\u0600-\u06FF])")
_DOUBLE_ZWNJ = re.compile(r"\u200c{2,}")

# Spaces around punctuation: "word ,word" → "word, word"; "( word )" → "(word)".
_PUNCT_PAD_BEFORE = re.compile(r" *([،؛:!؟%]) +")  # Persian punctuation
_PUNCT_PAD_BEFORE_NOFOLLOW = re.compile(r" *([،؛:!؟])")  # ensure none before
_PAREN_INNER = re.compile(r"\(\s*([^()]*?)\s*\)")
_BRACKET_INNER = re.compile(r"\[\s*([^\[\]]*?)\s*\]")

# Collapse any run of spaces (after exotic-space folding) into one.
_MULTI_SPACE = re.compile(r" {2,}")


@dataclass
class NormalizerConfig:
    """Toggles for :class:`Normalizer`."""

    digits: str = "persian"  # "persian" | "latin" | "keep"
    fix_zwnj: bool = True
    fix_spaces: bool = True
    canonicalize_letters: bool = True
    strip_dir_controls: bool = True
    fix_quotes: bool = True
    strip_diacritics: bool = True

    def validate(self) -> "NormalizerConfig":
        if self.digits not in ("persian", "latin", "keep"):
            raise ValueError(
                f"digits must be 'persian', 'latin' or 'keep', got {self.digits!r}"
            )
        return self


class Normalizer:
    """Apply Persian normalisation rules to text.

    Instances are cheap and stateless beyond their config; safe to share.
    """

    def __init__(self, config: NormalizerConfig | None = None) -> None:
        self.cfg = (config or NormalizerConfig()).validate()

    # -- public ----------------------------------------------------------- #
    def normalize(self, text: str) -> str:
        if not text:
            return ""

        # 1. Unicode NFKC first: folds compatibility forms (fullwidth digits,
        #    ligatures) so later steps see canonical code points.
        text = unicodedata.normalize("NFKC", text)

        if self.cfg.strip_dir_controls:
            text = "".join(ch for ch in text if ch not in _DIR_CONTROLS)

        if self.cfg.strip_diacritics:
            text = self._strip_diacritics(text)

        if self.cfg.canonicalize_letters:
            text = self._map_letters(text)

        # Letters/diacritics done → now handle digits.
        text = self._normalize_digits(text)

        if self.cfg.fix_quotes:
            text = self._fix_quotes(text)

        if self.cfg.fix_zwnj:
            text = self._fix_zwnj(text)

        if self.cfg.fix_spaces:
            text = self._fix_spaces(text)

        return text.rstrip()

    __call__ = normalize

    # -- search keys ------------------------------------------------------ #
    def search_key(self, text: str) -> str:
        """A lossy key for *matching*, not display.

        Normalises then strips ZWNJ and all whitespace and lower-cases Latin,
        so ``"می کند"``, ``"می‌کند"`` and ``"میکند"`` collide, and a Latin
        ``1400`` matches a Persian ``۱۴۰۰`` (``normalize`` already folds Latin
        digits to Persian by default). Use :meth:`numeric_search_key` for
        amounts that may carry grouping separators or currency words.
        """
        if not text:
            return ""
        s = self.normalize(text)
        s = s.replace(ZWNJ, "")
        s = re.sub(r"\s+", "", s)
        return s.lower()

    def numeric_search_key(self, text: str) -> str:
        """Like :meth:`search_key` but also drops grouping separators and
        common currency words/symbols so ``"۱٬۰۰۰٬۰۰۰٬۰۰۰"`` matches
        ``"1000000000"``."""
        s = self.search_key(text)
        for ch in ("٬", ",", "،", "﷼", "$", "€"):
            s = s.replace(ch, "")
        for word in ("ریال", "تومان", "rial", "toman"):
            s = s.replace(word, "")
        s = s.replace("٫", ".")  # Arabic decimal separator → dot
        return s

    # -- internals -------------------------------------------------------- #
    def _map_letters(self, text: str) -> str:
        for src, dst in _LETTER_MAP.items():
            if src in text:
                text = text.replace(src, dst)
        return text

    def _strip_diacritics(self, text: str) -> str:
        # Persian/Arabic diacritics (harakat) and combining marks. They rarely
        # carry retrieval signal and often appear inconsistently.
        return "".join(
            ch
            for ch in text
            if unicodedata.category(ch) != "Mn"
            and ch not in {
                "\u064b", "\u064c", "\u064d", "\u064e", "\u064f",
                "\u0650", "\u0651", "\u0652", "\u0653", "\u0654",
                "\u0670",  # superscript alef
            }
        )

    def _normalize_digits(self, text: str) -> str:
        # Arabic-Indic → Persian unconditionally (they are never meaningful in
        # Persian text as distinct from Persian digits).
        text = text.translate(_ARABIC_TO_PERSIAN)
        mode = self.cfg.digits
        if mode == "persian":
            text = text.translate(_LATIN_TO_PERSIAN)
        elif mode == "latin":
            text = text.translate(_PERSIAN_TO_LATIN)
        # mode == "keep": leave Latin as-is (already Arabic→Persian done).
        return text

    def _fix_quotes(self, text: str) -> str:
        for src, dst in _QUOTE_MAP.items():
            if src in text:
                text = text.replace(src, dst)
        return text

    def _fix_zwnj(self, text: str) -> str:
        # Collapse doubled ZWNJ and drop ZWNJ that is not between two Persian
        # letters (leading/trailing/standalone).
        text = _DOUBLE_ZWNJ.sub(ZWNJ, text)
        text = _LEADING_ZWNJ.sub("", text)
        text = _TRAILING_ZWNJ.sub("", text)

        # Replace misplaced plain spaces around known affixes with ZWNJ.
        # Repeat once: a second pass would only re-match what we just fixed.
        text = _PREFIX_SPACE_RE.sub(r"\1" + ZWNJ, text)
        text = _SUFFIX_SPACE_RE.sub(ZWNJ + r"\1", text)
        return text

    def _fix_spaces(self, text: str) -> str:
        # Fold exotic spaces to normal space, then dedupe.
        for sp in _EXOTIC_SPACES:
            if sp in text:
                text = text.replace(sp, " ")
        text = _MULTI_SPACE.sub(" ", text)

        # Persian punctuation should have no space before it, one after.
        text = _PUNCT_PAD_BEFORE.sub(r"\1 ", text)
        text = _PUNCT_PAD_BEFORE_NOFOLLOW.sub(r"\1", text)
        # "word ," → "word, " (ASCII comma handled here, Persian ، above).
        text = re.sub(r" *, +", ", ", text)
        # Parentheses/brackets: strip inner padding only, leave the outer
        # spacing intact so "متن (داخلی) بیرون" stays spaced but
        # "( پرانتز )" becomes "(پرانتز)".
        text = _PAREN_INNER.sub(lambda mo: "(" + mo.group(1).strip() + ")", text)
        text = _BRACKET_INNER.sub(lambda mo: "[" + mo.group(1).strip() + "]", text)
        # Trim spaces that punctuation tightening may have doubled.
        text = _MULTI_SPACE.sub(" ", text)
        return text.strip()


# A shared default instance and a module-level function for convenience.
_DEFAULT = Normalizer()


def normalize(text: str, config: NormalizerConfig | None = None) -> str:
    """Normalise ``text`` with the default retrieval config.

    Pass a :class:`NormalizerConfig` to override defaults (e.g. keep Latin
    digits in a financial context where ``1403`` should stay Latin).
    """
    if config is None:
        return _DEFAULT.normalize(text)
    return Normalizer(config).normalize(text)
