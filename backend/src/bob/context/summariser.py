"""Summariser module — feeds RAW older turns to a callable, never the prior digest.

PRD 0006 / issue 0046. The rolling summary in the bounded context must be
regenerated from RAW older turns each time it is rebuilt, *not* from the
prior digest. Going through prior digests is the canonical source of drift
in long-running assistants — every regeneration would lose a little
information and the digest would slowly become noise.

:class:`Summariser` is therefore a thin, injectable wrapper around a
"summarise this transcript" callable. Production wires in an LLM-backed
implementation (see :class:`LLMSummariser`); tests inject a deterministic
fake (see :class:`FixedTextSummariser`).

Both implementations follow the same contract:

* Input: a list of ``ContextEntry`` objects representing the RAW older
  turns to summarise (``user_turn`` + ``assistant_turn`` kinds).
* Output: a :class:`RollingSummary` carrying the rendered text, the
  ``summariser_version``, and the ``(from_turn, to_turn)`` range
  describing which turns were folded in.

The ``summariser_version`` is stamped on every persisted rolling summary
(see migration ``0006_rolling_summaries.sql``) so a future wording change
is visible at the data layer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bob.context.entry import ContextEntry
from bob.context.prompt_fragments import (
    SUMMARISER_SYSTEM_PROMPT,
    SUMMARISER_USER_PROMPT,
)

#: Stable summariser version. Bumped whenever the prompt or post-processing
#: changes in a way that materially alters output. Persisted alongside every
#: rolling summary.
SUMMARISER_VERSION = 1


@dataclass(frozen=True)
class RollingSummary:
    """Result of a single summariser invocation.

    Fields:

    - ``text`` — the rendered summary string (free-text, ≤ a few hundred
      tokens depending on the underlying impl).
    - ``summariser_version`` — :data:`SUMMARISER_VERSION` at generation
      time. Persisted so future re-reads can spot a stale digest produced
      by an older summariser revision.
    - ``from_turn`` / ``to_turn`` — 1-indexed range over user↔assistant turn
      pairs that were folded into ``text``. Both bounds are inclusive. When
      the range is empty (no turns to summarise) the :class:`Summariser`
      returns ``None`` rather than a :class:`RollingSummary` with
      ``from_turn > to_turn``.
    - ``raw_turn_count`` — count of RAW :class:`ContextEntry` rows actually
      fed to the summariser. Used by tests to assert the callable saw RAW
      turns and not the prior digest.
    """

    text: str
    summariser_version: int
    from_turn: int
    to_turn: int
    raw_turn_count: int


@runtime_checkable
class Summariser(Protocol):
    """Anything callable that maps RAW older turns to a :class:`RollingSummary`."""

    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:  # pragma: no cover — protocol member.
        ...


def render_transcript_for_summary(older_turns: Sequence[ContextEntry]) -> str:
    """Render ``older_turns`` as the transcript fed to the summariser.

    The rendering is the canonical "RAW turns" representation: each entry
    contributes one ``ROLE: content`` line in chronological order. Pure
    function — exposed at module level so tests can assert the exact
    transcript without re-implementing it.
    """

    lines: list[str] = []
    for entry in older_turns:
        role = entry.payload.get("role")
        content = entry.payload.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            # Defensive — bounded providers only emit role-tagged payloads.
            continue
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)


class FixedTextSummariser:
    """Deterministic summariser — useful for tests.

    Concatenates the rendered transcript with a small header. Production
    code never uses this; the orchestrator wires :class:`LLMSummariser` (or
    any other callable) at boot.

    The point of exposing it is two-fold:

    1. Tests get a side-effect-free implementation that lets them assert
       "the summariser saw the RAW older turns" via the rendered transcript.
    2. The minimal contract (``async def summarise(...)``) is documented
       through a working implementation.
    """

    def __init__(self, *, prefix: str = "SUMMARY") -> None:
        self._prefix = prefix

    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:
        if not older_turns or from_turn > to_turn:
            return None
        transcript = render_transcript_for_summary(older_turns)
        text = f"{self._prefix}[{from_turn}..{to_turn}]:\n{transcript}"
        return RollingSummary(
            text=text,
            summariser_version=SUMMARISER_VERSION,
            from_turn=from_turn,
            to_turn=to_turn,
            raw_turn_count=len(older_turns),
        )


#: Callable signature for the LLM-backed summariser's underlying chat
#: function. Matches :meth:`bob.llm_client.LLMClient.chat` but lifted to a
#: typing-only callable so :class:`LLMSummariser` does not depend on the
#: concrete client class.
LLMChatCallable = Callable[[list[dict[str, str]]], Awaitable[str]]


class LLMSummariser:
    """LLM-backed :class:`Summariser` that always summarises from RAW turns.

    Construction:

    - ``chat`` — async callable invoked with a ``messages`` list (system +
      user). Production wires :meth:`bob.llm_client.LLMClient.chat`
      (closed over the singleton client).

    The summariser builds the chat ``messages`` from the two prompt
    fragments (:data:`SUMMARISER_SYSTEM_PROMPT`, :data:`SUMMARISER_USER_PROMPT`)
    and the transcript rendered from ``older_turns``. Output is the raw
    string returned by ``chat`` with whitespace trimmed; if the LLM returns
    an empty string the summariser falls back to a single-line marker so
    the persisted row is never silently empty.
    """

    def __init__(self, *, chat: LLMChatCallable) -> None:
        self._chat = chat

    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:
        if not older_turns or from_turn > to_turn:
            return None

        transcript = render_transcript_for_summary(older_turns)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SUMMARISER_SYSTEM_PROMPT.template},
            {
                "role": "user",
                "content": SUMMARISER_USER_PROMPT.render(
                    from_turn=from_turn,
                    to_turn=to_turn,
                    transcript=transcript,
                ),
            },
        ]
        raw = await self._chat(messages)
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            text = f"(résumé vide pour les tours {from_turn}-{to_turn})"
        return RollingSummary(
            text=text,
            summariser_version=SUMMARISER_VERSION,
            from_turn=from_turn,
            to_turn=to_turn,
            raw_turn_count=len(older_turns),
        )
