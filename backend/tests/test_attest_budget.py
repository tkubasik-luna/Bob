"""Unit tests for the ``budget_refused`` attest assertion (PRD 0016 / issue 0107).

The assertion recognises a model-budget refusal (Annexe G "Budget dépassé") on
the black-box ``/ws/debug`` stream — either a ``payload.error ==
"budget_exceeded"`` marker or an ``error``-severity event whose summary mentions
the "plafond" refusal. These tests feed synthetic captured events directly; the
end-to-end scenario (a running backend triggering a real over-budget load) is
gated on per-role boot wiring landing in a later slice, exactly like the
``role_used_model`` seam. The budget-refusal invariant itself is attested today
by the focused integration test in ``test_role_budget.py``.
"""

from __future__ import annotations

from typing import Any

from bob.attest.assertions import AssertionContext, known_kinds, run_assertion


def _budget_event(role: str | None = None) -> dict[str, Any]:
    """A captured frame carrying the structured ``budget_exceeded`` error code."""

    payload: dict[str, Any] = {"error": "budget_exceeded", "detail": "dépasse le plafond"}
    if role is not None:
        payload["role"] = role
    return {"category": "llm", "severity": "info", "payload": payload}


def _ctx(*events: dict[str, Any]) -> AssertionContext:
    return AssertionContext(events=list(events), deliverable="")


def test_budget_refused_is_registered() -> None:
    assert "budget_refused" in known_kinds()


def test_budget_refused_pass_on_structured_error_code() -> None:
    ctx = _ctx(_budget_event())
    result = run_assertion({"kind": "budget_refused"}, ctx)
    assert result.ok is True
    assert result.detail["matched"] == 1


def test_budget_refused_pass_on_error_severity_plafond_summary() -> None:
    # The other recognised shape: an error-severity event mentioning "plafond".
    ctx = _ctx(
        {
            "category": "system",
            "severity": "error",
            "summary": "chargement refusé : dépasse le plafond mémoire",
            "payload": {},
        }
    )
    result = run_assertion({"kind": "budget_refused"}, ctx)
    assert result.ok is True


def test_budget_refused_fail_when_no_refusal_event() -> None:
    ctx = _ctx({"category": "output", "severity": "info", "payload": {"speech": "ok"}})
    result = run_assertion({"kind": "budget_refused"}, ctx)
    assert result.ok is False
    assert result.detail["matched"] == 0


def test_budget_refused_narrows_by_role() -> None:
    ctx = _ctx(_budget_event(role="draft"))
    # Matches the targeted role.
    assert run_assertion({"kind": "budget_refused", "role": "draft"}, ctx).ok is True
    # A different role does not match a role-tagged refusal.
    assert run_assertion({"kind": "budget_refused", "role": "jarvis"}, ctx).ok is False


def test_budget_refused_untagged_event_matches_any_role_query() -> None:
    # An untagged refusal (no role on the payload) matches a role-narrowed query
    # too — the harness should not miss a refusal just because the emit site did
    # not tag the role.
    ctx = _ctx(_budget_event(role=None))
    assert run_assertion({"kind": "budget_refused", "role": "draft"}, ctx).ok is True
