"""Tests for :class:`bob.context.providers.thinker_state.ThinkerStateProvider`.

PRD 0016 / issue 0102 — the PURE Speaker-consult provider. Asserts the snapshot
→ :class:`ContextEntry` projection (golden-prompt style via the assembler) plus
the no-op behaviours that keep the bounded text path byte-for-byte unchanged.
"""

from __future__ import annotations

from bob.context.assembler import ContextAssembler
from bob.context.policy import ContextPolicy
from bob.context.provider import AssemblyContext
from bob.context.providers.thinker_state import (
    THINKER_STATE_PROVIDER_ID,
    ThinkerStateProvider,
)
from bob.live_transcript_state import LiveTranscriptState, ThinkerSnapshot

from ._harness.golden_prompt import assert_matches_snapshot


def _assembly_ctx() -> AssemblyContext:
    return AssemblyContext(
        policy=ContextPolicy(
            policy_id="thinker_only",
            provider_ids=(THINKER_STATE_PROVIDER_ID,),
        ),
        user_message=None,
    )


def _state_with(snapshot: ThinkerSnapshot) -> LiveTranscriptState:
    store = LiveTranscriptState()
    store.update(snapshot)
    return store


def test_provider_id_is_stable() -> None:
    provider = ThinkerStateProvider(live_state=LiveTranscriptState())
    assert provider.provider_id == THINKER_STATE_PROVIDER_ID


def test_empty_store_emits_no_entry() -> None:
    provider = ThinkerStateProvider(live_state=LiveTranscriptState())
    assert list(provider.entries(_assembly_ctx())) == []


def test_snapshot_projects_to_pinned_system_entry() -> None:
    store = _state_with(
        ThinkerSnapshot(
            turn_id="t1",
            seq=2,
            corrected_text="quel temps fait-il à Paris",
            variables={"city": "Paris", "intent": "weather"},
            next_step_plan="donner la météo de Paris",
        )
    )
    provider = ThinkerStateProvider(live_state=store)
    entries = list(provider.entries(_assembly_ctx()))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.provider_id == THINKER_STATE_PROVIDER_ID
    assert entry.pinned is True
    assert entry.payload["role"] == "system"
    # The id is turn + seq scoped so it is unique per snapshot.
    assert entry.id == f"{THINKER_STATE_PROVIDER_ID}:turn-t1:seq-2"
    content = entry.payload["content"]
    assert "quel temps fait-il à Paris" in content
    assert "donner la météo de Paris" in content
    # variables are rendered with sorted keys (deterministic).
    assert '{"city": "Paris", "intent": "weather"}' in content


def test_carry_only_signals_not_rendered_into_prompt() -> None:
    """``user_turn_complete`` / ``backchannel`` steer the FSM, not the Speaker wording."""

    store = _state_with(
        ThinkerSnapshot(
            turn_id="t1",
            seq=1,
            corrected_text="bonjour",
            user_turn_complete=True,
            backchannel="mm",
        )
    )
    provider = ThinkerStateProvider(live_state=store)
    entries = list(provider.entries(_assembly_ctx()))
    content = entries[0].payload["content"]
    assert "user_turn_complete" not in content
    assert "backchannel" not in content
    assert "mm" not in content


def test_blank_snapshot_emits_nothing() -> None:
    """A snapshot with no corrected text, no variables, no plan projects nothing."""

    store = _state_with(ThinkerSnapshot(turn_id="t1", seq=1))
    provider = ThinkerStateProvider(live_state=store)
    assert list(provider.entries(_assembly_ctx())) == []


def test_only_variables_still_projects() -> None:
    store = _state_with(ThinkerSnapshot(turn_id="t1", seq=1, variables={"intent": "greet"}))
    provider = ThinkerStateProvider(live_state=store)
    entries = list(provider.entries(_assembly_ctx()))
    assert len(entries) == 1
    assert '{"intent": "greet"}' in entries[0].payload["content"]


def test_latest_snapshot_wins_in_projection() -> None:
    """The provider projects the freshest accepted snapshot (anti-stale interplay)."""

    store = LiveTranscriptState()
    store.update(ThinkerSnapshot(turn_id="t1", seq=1, corrected_text="première lecture"))
    store.update(ThinkerSnapshot(turn_id="t1", seq=2, corrected_text="lecture finale"))
    # A stale arrival must not win.
    store.update(ThinkerSnapshot(turn_id="t1", seq=1, corrected_text="stale"))
    provider = ThinkerStateProvider(live_state=store)
    entries = list(provider.entries(_assembly_ctx()))
    content = entries[0].payload["content"]
    assert "lecture finale" in content
    assert "première lecture" not in content
    assert "stale" not in content


def test_golden_prompt_projection() -> None:
    """Pin the assembled THINKER block layout (golden-prompt harness)."""

    store = _state_with(
        ThinkerSnapshot(
            turn_id="turn-abc",
            seq=3,
            corrected_text="réserve une table pour deux à vingt heures",
            variables={"action": "reservation", "covers": 2, "time": "20:00"},
            next_step_plan="confirmer la réservation",
        )
    )
    assembler = ContextAssembler(
        providers=[ThinkerStateProvider(live_state=store)],
        policy=ContextPolicy(
            policy_id="thinker_only",
            provider_ids=(THINKER_STATE_PROVIDER_ID,),
        ),
    )
    messages = assembler.assemble(user_message=None)
    assert_matches_snapshot(messages, "thinker_state_block")
