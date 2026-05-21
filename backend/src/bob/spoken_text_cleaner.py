"""Markdown → spoken-text cleanup for TTS (issue 0012).

Pure, deterministic transform applied to the assistant's ``speech`` field
*before* :mod:`bob.text_segmenter` sees it, so Bob doesn't read raw markdown
aloud and code blocks don't produce phantom sentences.

Public API
----------

- :func:`clean_for_speech` — return a cleaned copy of ``text`` suitable for TTS.

Frontend rendering is untouched: only what flows into ``tts_service`` is
modified.

URL policy
----------

URLs matching ``https?://\\S+`` are **stripped entirely** (replaced with an
empty string). Rationale: reading a URL character-by-character is worse than
silence; the visible chat still shows the link. Alternative ("lien") was
considered and rejected — silence is less intrusive for the common case where
the link is incidental to the prose.

Order of operations
-------------------

1. Strip triple-backtick fenced code blocks (``` ``` … ``` ```), greedy in
   ``DOTALL`` but non-greedy across blocks. Done **first** so emphasis /
   bullet rules can't accidentally rewrite code-block content.
2. Strip URLs.
3. Strip leading-line headings (``^#{1,6}\\s+``).
4. Strip list-bullet prefixes (``-``, ``*``, ``+``, ``1.``, ``2.`` …) at
   line start — done **before** emphasis so a leading ``*`` bullet is not
   mistaken for emphasis.
5. Strip emphasis: ``**x**``, ``__x__`` → ``x``; ``*x*``, ``_x_`` → ``x``;
   ``~~x~~`` → ``x``. Non-greedy.
6. Strip inline backticks: `` `code` `` → ``code`` (keep the inner text).
7. Per-line whitespace collapse: runs of spaces/tabs → single space, trim
   each line. Paragraph breaks (``\\n\\n``) preserved so the segmenter still
   sees them.

All rules are regex-based; the transform is O(n) in the input length and has
no side effects.
"""

from __future__ import annotations

import re

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_UNORDERED_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_ORDERED_BULLET_RE = re.compile(r"^[ \t]*\d+\.[ \t]+", re.MULTILINE)
_STRONG_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRONG_UNDER_RE = re.compile(r"__(.+?)__", re.DOTALL)
_EM_STAR_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_EM_UNDER_RE = re.compile(r"(?<!\w)_(.+?)_(?!\w)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_INLINE_BACKTICK_RE = re.compile(r"`([^`]*)`")
_INLINE_WS_RE = re.compile(r"[ \t]+")


def clean_for_speech(text: str) -> str:
    """Return ``text`` rewritten for spoken delivery.

    Pure function. See module docstring for the rule list and URL policy.
    Empty / whitespace-only input returns ``""``.
    """

    if not text or not text.strip():
        return ""

    # 1. Drop fenced code blocks entirely (replace with single space so adjacent
    #    prose doesn't get glued together).
    out = _CODE_FENCE_RE.sub(" ", text)

    # 2. Strip URLs.
    out = _URL_RE.sub("", out)

    # 3. Headings.
    out = _HEADING_RE.sub("", out)

    # 4. List bullets (before emphasis so a leading ``*`` isn't mistaken for it).
    out = _UNORDERED_BULLET_RE.sub("", out)
    out = _ORDERED_BULLET_RE.sub("", out)

    # 5. Emphasis. Order matters: ``**`` before ``*``, ``__`` before ``_``.
    out = _STRONG_STAR_RE.sub(r"\1", out)
    out = _STRONG_UNDER_RE.sub(r"\1", out)
    out = _STRIKE_RE.sub(r"\1", out)
    out = _EM_STAR_RE.sub(r"\1", out)
    out = _EM_UNDER_RE.sub(r"\1", out)

    # 6. Inline backticks — keep the inner text, drop the ticks.
    out = _INLINE_BACKTICK_RE.sub(r"\1", out)

    # 7. Per-line whitespace collapse; preserve paragraph breaks.
    lines = out.split("\n")
    cleaned_lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in lines]
    out = "\n".join(cleaned_lines)

    # Collapse 3+ consecutive newlines down to the canonical paragraph break.
    out = re.sub(r"\n{3,}", "\n\n", out)

    return out.strip()
