"""Minimal :class:`Tokenizer` interface used by bounded providers.

PRD 0006 / issue 0046. Providers and the long-session smoke test need a
single ``Tokenizer.count(text)`` entry point so they can enforce budgets
without depending on a specific model tokenizer (LM Studio today, swap
later). The default :class:`WordCountTokenizer` is dependency-free and
deterministic — sufficient for tests that only need *some* monotonic
length signal.

Issue 0049 will swap in a real tokenizer (``tiktoken`` or a model-specific
tokenizer behind the same protocol). The interface stays stable.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

#: Module-level regex matches "tokens" as whitespace-separated runs. Good
#: enough for budgeting heuristics; not byte-accurate.
_WORD_TOKEN_RE = re.compile(r"\S+")


@runtime_checkable
class Tokenizer(Protocol):
    """Anything with a ``count(text) -> int`` is a tokenizer for our purposes."""

    def count(self, text: str) -> int:  # pragma: no cover — protocol member.
        ...


class WordCountTokenizer:
    """Default :class:`Tokenizer` — count whitespace-separated tokens.

    Cheap (no allocation), deterministic and dependency-free. Bob's bounded
    providers use the result as a rough budget signal, not a billing-grade
    count. Compared to a tiktoken-style BPE this typically undercounts by
    ~25 % which is fine for "plateau at K-ish tokens" assertions.
    """

    def count(self, text: str) -> int:
        if not text:
            return 0
        return sum(1 for _ in _WORD_TOKEN_RE.finditer(text))


def default_tokenizer() -> Tokenizer:
    """Return the process-wide default :class:`Tokenizer`.

    Wrapped behind a function so tests and future call sites can pin a
    specific implementation without rewriting imports.
    """

    return WordCountTokenizer()
