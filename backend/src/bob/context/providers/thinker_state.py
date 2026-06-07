"""ThinkerStateProvider — project the latest Thinker snapshot into the prompt.

PRD 0016 / issue 0102 (Annexe A.2 + the « Penser en parallèle » étage). While
the user is still speaking, the background :class:`bob.thinker_loop.ThinkerLoop`
maintains a running understanding of the turn in
:class:`bob.live_transcript_state.LiveTranscriptState`. When the FSM endpoint
fires and the Speaker (Jarvis say-path) assembles its prompt, THIS provider
reads the latest :class:`bob.live_transcript_state.ThinkerSnapshot` and emits a
single ``role=system`` :class:`ContextEntry` — the "Restate-Consult-Solve"
context the Speaker answers from.

Pure, exactly like :class:`bob.context.providers.state_block.StateBlockProvider`
----------------------------------------------------------------------------

:meth:`entries` does **no I/O, no time, no random**. It only reads the in-memory
:class:`LiveTranscriptState` (immutable for the duration of the assembly) and
renders a deterministic string — same snapshot in → same entry out — so the
golden-prompt harness can pin the layout. The store/loop own all the effects
(LLM calls, debouncing, the WS event); the provider is a side-effect-free
projection, which is what lets the per-turn assembly stay pure even though the
data behind it is produced by a live background loop.

Shape (deterministic, byte-stable)
----------------------------------

When the store holds a snapshot, the provider renders a compact multi-line
THINKER block carrying the ``corrected_text`` (what the user is saying),
the extracted ``variables`` (intent / entities, JSON with sorted keys so the
line is stable), and the ``next_step_plan``. The carry-only signals
(``user_turn_complete`` / ``backchannel``) are NOT rendered into the prompt —
they steer the FSM / backchannel layers (S7/0103, S10/0105), not the Speaker's
wording — so they stay off the assembled context. An empty store (no Thinker
pass yet) emits nothing, so a turn that endpoints before the loop produced a
snapshot degrades cleanly to the rest of the bounded prompt.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.provider import AssemblyContext
from bob.live_transcript_state import LiveTranscriptState, ThinkerSnapshot

#: Stable provider id used by the assembler registry + the policy provider list.
THINKER_STATE_PROVIDER_ID = "thinker_state"


class ThinkerStateProvider:
    """Compose the THINKER block from the latest live-transcript snapshot.

    Construction args:

    - ``live_state`` — the :class:`LiveTranscriptState` the Thinker loop writes.
      The provider holds a reference and reads :meth:`LiveTranscriptState.latest`
      at assembly; it never mutates the store.

    Mirrors :class:`StateBlockProvider`: a stable ``provider_id`` property + a
    pure :meth:`entries` that returns ``[]`` when there is nothing to project.
    """

    def __init__(self, *, live_state: LiveTranscriptState) -> None:
        self._live_state = live_state

    @property
    def provider_id(self) -> str:
        return THINKER_STATE_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        snapshot = self._live_state.latest()
        if snapshot is None:
            return []
        rendered = _render_thinker_block(snapshot)
        if not rendered:
            return []
        return [
            ContextEntry(
                id=f"{THINKER_STATE_PROVIDER_ID}:turn-{snapshot.turn_id}:seq-{snapshot.seq}",
                kind="system_note",
                source="thinker_state_provider",
                token_estimate=len(rendered) // 4,
                pinned=True,
                created_at="",
                provider_id=THINKER_STATE_PROVIDER_ID,
                payload={"role": "system", "content": rendered},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]


def _render_thinker_block(snapshot: ThinkerSnapshot) -> str:
    """Render the snapshot as a single multi-line system message (deterministic).

    Same inputs → same string. ``variables`` are serialised with
    ``sort_keys=True`` so dict-ordering never perturbs the golden snapshot. The
    carry-only ``user_turn_complete`` / ``backchannel`` signals are deliberately
    omitted from the rendered prompt — they drive the FSM/backchannel layers,
    not the Speaker's phrasing. Returns ``""`` when the snapshot carries no
    usable understanding (no corrected text, no variables, no plan) so the
    provider emits no empty block.
    """

    corrected = " ".join(snapshot.corrected_text.split())
    plan = " ".join(snapshot.next_step_plan.split())
    has_variables = bool(snapshot.variables)
    if not corrected and not plan and not has_variables:
        return ""

    lines = [
        "THINKER (compréhension en cours du tour, PRD 0016). État maintenu en "
        "parallèle de la parole de l'utilisateur :"
    ]
    if corrected:
        lines.append(f'- texte_corrigé: "{corrected}"')
    if has_variables:
        variables_repr = json.dumps(snapshot.variables, ensure_ascii=False, sort_keys=True)
        lines.append(f"- variables: {variables_repr}")
    if plan:
        lines.append(f'- plan_prochaine_étape: "{plan}"')
    lines.append("Appuie-toi sur cette compréhension pour répondre directement et à propos.")
    return "\n".join(lines)


__all__ = [
    "THINKER_STATE_PROVIDER_ID",
    "ThinkerStateProvider",
]
