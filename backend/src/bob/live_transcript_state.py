"""In-memory store of the Thinker's running understanding of a turn.

PRD 0016 / issue 0102 (Annexe A.2 + H). The « Penser en parallèle » étage runs a
mini reasoning loop (:class:`bob.thinker_loop.ThinkerLoop`) over the partial
transcript while the user is still speaking. Each pass produces a
:class:`ThinkerSnapshot` — a structured read of what the user wants so far
(``corrected_text`` / ``variables`` / ``next_step_plan``) plus two carry-only
signals consumed by later slices (``user_turn_complete`` for the semantic
endpoint, S7/0103; ``backchannel`` for the pause acknowledgements, S10/0105).

:class:`LiveTranscriptState` is the blackboard the loop WRITES and the
**pure** :class:`bob.context.providers.thinker_state.ThinkerStateProvider` READS
at prompt-assembly time — exactly the producer/consumer split
:class:`bob.context.providers.state_block.StateBlockProvider` has against
:class:`bob.task_store.TaskStore`. Keeping the store a plain in-memory object
(no I/O, no time) means the provider that reads it stays pure and the assembly
remains snapshot-stable for the golden-prompt tests.

Anti-stale ordering (Annexe H, normative)
------------------------------------------

Thinker passes are debounced and may finish out of order (a longer pass started
earlier can land after a shorter later one). Every snapshot therefore carries a
monotonic ``seq`` minted by the loop; :meth:`LiveTranscriptState.update` IGNORES
a snapshot whose ``seq`` is **not strictly greater** than the last accepted one.
This guarantees the store never regresses to an older understanding — the
provider always reads the freshest snapshot the loop produced, never a stale
late arrival.

The store is per-session (one live turn at a time in the full-duplex loop). It
is deliberately tiny: hold the latest snapshot, accept-or-ignore on ``seq``,
and clear at turn boundaries. Nothing here knows about the LLM, the FSM or the
WebSocket — those are the loop's concern.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ThinkerSnapshot:
    """One pass of the Thinker's understanding of the current turn (Annexe A.2).

    Fields mirror the ``thinker_snapshot`` event payload (Annexe A.2):

    - ``turn_id`` — the FSM turn this snapshot belongs to (correlation).
    - ``seq`` — monotonic per-turn sequence number minted by the loop. A
      snapshot with ``seq`` not strictly greater than the last accepted one is
      ignored by :class:`LiveTranscriptState` (anti-stale).
    - ``corrected_text`` — the loop's cleaned/punctuated reading of the partial
      transcript (the model's best guess at what the user actually said so far).
    - ``variables`` — structured slots the loop extracted (intent, entities,
      parameters). Free-form ``{str: Any}`` so the mini model can populate
      whatever the prompt asks for without a rigid schema.
    - ``next_step_plan`` — a one-line plan for what Bob should do next, so the
      Speaker can consult it at assembly.
    - ``user_turn_complete`` — the semantic-endpoint signal. CARRIED here; the
      confirmation logic that actually fires the endpoint lives in S7/0103. The
      VAD silence floor remains the endpoint net until then.
    - ``backchannel`` — a pause-acknowledgement trigger. CARRIED here; consumed
      in S10/0105. ``None`` when the loop has nothing to interject.
    """

    turn_id: str
    seq: int
    corrected_text: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    next_step_plan: str = ""
    user_turn_complete: bool = False
    backchannel: str | None = None


class LiveTranscriptState:
    """Per-session blackboard holding the latest :class:`ThinkerSnapshot`.

    The Thinker loop writes via :meth:`update`; the pure
    :class:`ThinkerStateProvider` reads via :meth:`latest` at assembly. A
    :class:`threading.Lock` serialises the (rare) concurrent read/write so the
    provider can be invoked from the orchestrator's request worker while the
    loop's asyncio task writes from the event-loop thread without tearing a
    snapshot.

    The store starts empty (``latest()`` is ``None``) so a turn with no Thinker
    pass yet projects no context entry — the provider degrades to a no-op,
    exactly like :class:`StateBlockProvider` on an empty :class:`TaskStore`.
    """

    def __init__(self) -> None:
        self._latest: ThinkerSnapshot | None = None
        self._last_seq: int = -1
        self._lock = threading.Lock()

    def update(self, snapshot: ThinkerSnapshot) -> bool:
        """Accept ``snapshot`` iff its ``seq`` is strictly newer (anti-stale).

        Returns ``True`` when the snapshot was accepted (it becomes the new
        :meth:`latest`), ``False`` when it was IGNORED because its ``seq`` is
        not strictly greater than the last accepted one (a stale late arrival,
        Annexe H). The caller (the loop) uses the return to decide whether to
        emit the ``thinker_snapshot`` event — a dropped snapshot emits nothing.
        """

        with self._lock:
            if snapshot.seq <= self._last_seq:
                return False
            self._latest = snapshot
            self._last_seq = snapshot.seq
            return True

    def latest(self) -> ThinkerSnapshot | None:
        """Return the freshest accepted snapshot, or ``None`` when empty.

        Pure read — no mutation, no time. The provider calls this at assembly.
        """

        with self._lock:
            return self._latest

    def clear(self) -> None:
        """Reset the store at a turn boundary (``endpoint`` / ``voice_stop``).

        Drops the held snapshot AND the ``seq`` watermark so the NEXT turn's
        first snapshot (which restarts its own ``seq`` from 0) is accepted. The
        full-duplex loop calls this when it freezes a turn / tears the loop
        down, so a new turn never inherits the previous turn's understanding.
        """

        with self._lock:
            self._latest = None
            self._last_seq = -1


__all__ = ["LiveTranscriptState", "ThinkerSnapshot"]
