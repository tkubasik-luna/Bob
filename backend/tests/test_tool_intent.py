"""Unit tests for :mod:`bob.tool_intent` — the Draft's tool-intent gate (0104).

The predicate classifies a voice partial as a TOOL turn (stay cold, no draft)
when any sub-agent tool clears the ``select_tools`` lexical threshold. Pure +
offline: scored against the default registry (gmail + web tools) plus a tiny
hand-built one for the threshold edges.
"""

from __future__ import annotations

import pytest

from bob.sub_agent.tool_registry import build_default_subagent_registry
from bob.tool_intent import build_tool_intent_predicate


def test_tool_intent_flags_a_weather_turn_against_the_default_registry() -> None:
    # ``web_search`` carries the ``météo`` retrieval tag (PRD 0015 / issue 0092)
    # — the same lexical hit that would advertise the tool to a sub-agent must
    # keep the Draft cold.
    predicate = build_tool_intent_predicate(build_default_subagent_registry(), min_score=1)
    assert predicate("quelle est la météo à paris demain") is True


def test_tool_intent_flags_a_mail_turn_against_the_default_registry() -> None:
    predicate = build_tool_intent_predicate(build_default_subagent_registry(), min_score=1)
    assert predicate("est-ce que j'ai reçu un mail de jean") is True


def test_tool_intent_ignores_a_conversational_turn() -> None:
    predicate = build_tool_intent_predicate(build_default_subagent_registry(), min_score=1)
    assert predicate("raconte-moi une blague") is False


def test_tool_intent_ignores_empty_and_whitespace_partials() -> None:
    predicate = build_tool_intent_predicate(build_default_subagent_registry(), min_score=1)
    assert predicate("") is False
    assert predicate("   ") is False


def test_tool_intent_floors_the_threshold_at_one() -> None:
    # A zero/negative knob must not flag every turn (every tool scores >= 0).
    predicate = build_tool_intent_predicate(build_default_subagent_registry(), min_score=0)
    assert predicate("raconte-moi une blague") is False


def test_tool_intent_sees_tools_registered_after_construction() -> None:
    # The predicate closes over the registry OBJECT and reads it at call time —
    # the MCP fleet lands during boot, after the predicate is built.
    registry = build_default_subagent_registry()
    predicate = build_tool_intent_predicate(registry, min_score=1)
    assert predicate("allume la lampe du salon") is False

    import dataclasses

    from bob.sub_agent.tool_registry import build_web_search_tool

    lamp_tool = dataclasses.replace(
        build_web_search_tool(), name="lampe_salon", tags=("lampe", "salon", "lumière")
    )
    registry.register(lamp_tool)
    assert predicate("allume la lampe du salon") is True


def test_ws_router_wires_the_provider_predicate_into_the_drafter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The voice_start drafter factory passes the provider's predicate through.

    Regression for the 0104 gap where ``_make_speculative_draft`` built the
    drafter WITHOUT ``is_tool_intent`` — tool turns were drafted and a committed
    draft could replace the tool dispatch entirely.
    """

    from bob import ws_router
    from bob.config import Settings

    monkeypatch.setattr(
        "bob.llm.factory.build_draft_role_client", lambda role_selection, settings: object()
    )
    settings = Settings.model_construct(
        LLM_PROVIDER="lm_studio",
        LLM_MODEL="m",
        LLM_BASE_URL="",
        THINKER_DEBOUNCE_MS=250,
        THINKER_CANCEL_GRACE_MS=50,
        THINKER_CANCEL_GRACE_CAP_MS=250,
        DRAFT_COMMIT_SIMILARITY=0.6,
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )

    def sentinel_predicate(_text: str) -> bool:
        return True

    ws_router.set_tool_intent_provider(lambda: sentinel_predicate)
    try:
        drafter = ws_router._make_speculative_draft("s1", settings)
        assert drafter is not None
        assert drafter._is_tool_intent is sentinel_predicate
    finally:
        ws_router.reset_tool_intent_provider()

    # Default provider (bare boot / tests): no predicate — always speculate.
    drafter = ws_router._make_speculative_draft("s1", settings)
    assert drafter is not None
    assert drafter._is_tool_intent is None
