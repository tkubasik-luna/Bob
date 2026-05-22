# ruff: noqa: RUF001, RUF003
"""FR text normalization for TTS — upfront, deterministic, lossless.

Sits between :mod:`bob.spoken_text_cleaner` (markdown stripping) and
:mod:`bob.text_segmenter` (sentence splitting). Purpose: rewrite characters
that misaki[fr] + espeak handle inconsistently into their nearest reliable
equivalents, *without* dropping words.

The previous implementation caught the
``"number of lines in input and output must be equal"`` phonemizer error
post-facto and aggressively stripped punctuation — often losing characters
that mattered. This module does the inverse: replace problematic chars
with safe equivalents upfront so the phonemizer never raises.

Rules (in order):

1. Unicode NFC normalize so combining marks collapse to single codepoints.
2. Replace curly/typographic quotes with ASCII ``"`` / ``'``.
3. Replace em/en/figure/horizontal dashes with ``, `` (espeak treats bare
   dash inconsistently between FR voices).
4. Replace ellipsis (``\\u2026``) and runs of ASCII dots with a single ``.``.
5. Replace non-breaking / thin / zero-width / ideographic spaces with ASCII space.
6. Replace bullets / mid-dots with ``,``.
7. Strip everything outside the safe set (letters incl. accented, digits,
   whitespace, ``.,;:!?'-/%`` and ``€£$()``). Emoji, math symbols,
   line/box drawing, etc. are removed silently.
8. Collapse intra-line whitespace runs to a single space; preserve paragraph
   breaks (``\\n\\n``) and collapse triples+ to a paragraph break.
"""

from __future__ import annotations

import re
import unicodedata

_QUOTE_DOUBLE = str.maketrans(
    {
        "«": '"',  # «
        "»": '"',  # »
        "“": '"',  # “
        "”": '"',  # ”
        "„": '"',  # „
        "‟": '"',  # ‟
        "❝": '"',  # ❝
        "❞": '"',  # ❞
        "＂": '"',  # ＂
    }
)

_QUOTE_SINGLE = str.maketrans(
    {
        "‘": "'",  # ‘
        "’": "'",  # ’
        "‚": "'",  # ‚
        "‛": "'",  # ‛
        "❛": "'",  # ❛
        "❜": "'",  # ❜
        "´": "'",  # ´
        "`": "'",  # `
    }
)

# Em, en, figure, horizontal bar, minus.
_DASH_RE = re.compile("[‒–—―−]")

_ELLIPSIS_RE = re.compile("…")

# All non-newline whitespace flavors → plain space.
_INVISIBLE_SPACE_RE = re.compile(
    "["
    " "  # NBSP
    " -​"  # en quad … zero-width space
    " "  # narrow NBSP
    " "  # medium math space
    "　"  # ideographic space
    "﻿"  # BOM / zero-width no-break space
    "]"
)

# Decorative punctuation → comma.
_DECORATIVE_RE = re.compile(
    "[•·●◦▪■□◆◇"
    "►▶▸‣⁃]"
)

# Safe character set: word chars (incl. unicode letters/digits), whitespace,
# and the punctuation espeak/misaki handle reliably + currency / parens.
_SAFE_CHARS_RE = re.compile(
    r"[^\w\s.,;:!?'\-/%()" "€£$" "]",
    re.UNICODE,
)

_MULTI_DOT_RE = re.compile(r"\.{2,}")
_INLINE_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def normalize_for_tts(text: str) -> str:
    """Return ``text`` with TTS-hostile characters rewritten to safe equivalents.

    Idempotent: ``normalize_for_tts(normalize_for_tts(x)) == normalize_for_tts(x)``.
    Empty / whitespace-only input returns ``""``.
    """

    if not text or not text.strip():
        return ""

    out = unicodedata.normalize("NFC", text)
    out = out.translate(_QUOTE_DOUBLE)
    out = out.translate(_QUOTE_SINGLE)
    out = _DASH_RE.sub(", ", out)
    out = _ELLIPSIS_RE.sub(".", out)
    out = _INVISIBLE_SPACE_RE.sub(" ", out)
    out = _DECORATIVE_RE.sub(",", out)
    out = _SAFE_CHARS_RE.sub(" ", out)
    out = _MULTI_DOT_RE.sub(".", out)

    lines = out.split("\n")
    cleaned = [_INLINE_WS_RE.sub(" ", line).strip() for line in lines]
    out = "\n".join(cleaned)
    out = _MULTI_NL_RE.sub("\n\n", out)

    return out.strip()
