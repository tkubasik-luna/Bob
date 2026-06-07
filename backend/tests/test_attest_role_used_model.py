"""Unit tests for the ``role_used_model`` attest assertion (PRD 0016 / issue 0106).

The assertion reads the served model off captured ``category="llm"`` debug
events (the only surface a served model has on the black-box ``/ws/debug``
stream). These tests feed synthetic captured events to the assertion directly —
the end-to-end scenario (a real turn pinning ``jarvis=lm_studio:modelA``) is
gated on per-role boot wiring landing in a later slice, so it is not exercised
here; the per-role *machinery* is covered by ``test_role_factory.py`` /
``test_role_swap.py`` / ``test_role_router.py``.
"""

from __future__ import annotations

from typing import Any

from bob.attest.assertions import AssertionContext, known_kinds, run_assertion


def _llm_event(model: str) -> dict[str, Any]:
    """A captured ``/ws/debug`` frame for an LLM call serving ``model``."""

    return {
        "category": "llm",
        "severity": "info",
        "source": "bob.llm_client.complete",
        "summary": f"LLM call démarré (model={model})",
        "payload": {"model": model, "tokens_prompt_estimate": 12},
    }


def _ctx(*events: dict[str, Any]) -> AssertionContext:
    return AssertionContext(events=list(events), deliverable="ok")


def test_role_used_model_is_registered() -> None:
    assert "role_used_model" in known_kinds()


def test_role_used_model_pass_when_model_served() -> None:
    ctx = _ctx(_llm_event("modelA"))
    result = run_assertion({"kind": "role_used_model", "role": "jarvis", "model": "modelA"}, ctx)
    assert result.ok is True
    assert result.detail["role"] == "jarvis"
    assert result.detail["models_seen"] == ["modelA"]


def test_role_used_model_fail_on_wrong_model() -> None:
    ctx = _ctx(_llm_event("modelB"))
    result = run_assertion({"kind": "role_used_model", "role": "jarvis", "model": "modelA"}, ctx)
    assert result.ok is False
    assert result.detail["expected_model"] == "modelA"
    assert result.detail["models_seen"] == ["modelB"]


def test_role_used_model_fail_when_no_llm_calls() -> None:
    # Only a non-LLM event (e.g. an ``output`` say) — no served model surfaces.
    ctx = _ctx({"category": "output", "payload": {"speech": "salut"}})
    result = run_assertion({"kind": "role_used_model", "role": "jarvis", "model": "modelA"}, ctx)
    assert result.ok is False
    assert result.detail["models_seen"] == []


def test_role_used_model_requires_model_field() -> None:
    ctx = _ctx(_llm_event("modelA"))
    result = run_assertion({"kind": "role_used_model", "role": "jarvis"}, ctx)
    assert result.ok is False
    assert "error" in result.detail


def test_role_used_model_dedupes_seen_models() -> None:
    ctx = _ctx(_llm_event("modelA"), _llm_event("modelA"), _llm_event("modelB"))
    result = run_assertion({"kind": "role_used_model", "role": "jarvis", "model": "modelA"}, ctx)
    assert result.ok is True
    assert result.detail["models_seen"] == ["modelA", "modelB"]
