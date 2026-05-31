"""Tests for the agent-activity chip taxonomy (PRD 0011 / issue 0071).

Three surfaces, scoped to the PURE :mod:`bob.sub_agent.activity_projector`:

1. Taxonomy: each internal event kind maps to the expected
   ``{agent_ref, kind, label, status}`` descriptor; tool_call / ask_user /
   incidents (stall / cap / retry / validation_failed) all present.
2. Aggregation: a PASSING validation produces NO chip (suppressed), while a
   FAILING one surfaces as a salient ``error`` chip.
3. Redaction: a Mail-bearing free-text event (an ``ask_user`` question echoing a
   subject, a structured Mail payload) is scrubbed — no subject / snippet leaks
   into the projected descriptor.
"""

from __future__ import annotations

from typing import Any

from bob.sub_agent.activity_projector import (
    AgentActivity,
    AskUser,
    CapReached,
    Retry,
    StallNudge,
    TaskFinished,
    TaskStarted,
    ToolCallFinished,
    ToolCallStarted,
    Validation,
    project,
    redact_payload,
)

AGENT = "task-123"


# ---------------------------------------------------------------------------
# 1. Taxonomy
# ---------------------------------------------------------------------------


def test_task_started_maps_to_started_info_chip() -> None:
    chip = project(TaskStarted(agent_ref=AGENT, title="Lire le mail"))
    assert chip is not None
    assert chip.agent_ref == AGENT
    assert chip.kind == "started"
    assert chip.status == "info"
    assert "Lire le mail" in chip.label


def test_task_started_without_title() -> None:
    chip = project(TaskStarted(agent_ref=AGENT))
    assert chip is not None
    assert chip.kind == "started"
    assert chip.label == "Démarré"


def test_task_finished_complete_and_degraded_are_ok() -> None:
    for status in ("complete", "degraded"):
        chip = project(TaskFinished(agent_ref=AGENT, status=status))
        assert chip is not None
        assert chip.kind == "finished"
        assert chip.status == "ok"


def test_task_finished_failure_states_are_error() -> None:
    for status in ("failed", "cancelled", "timeout"):
        chip = project(TaskFinished(agent_ref=AGENT, status=status))
        assert chip is not None
        assert chip.kind == "finished"
        assert chip.status == "error"


def test_tool_call_started_is_running() -> None:
    chip = project(ToolCallStarted(agent_ref=AGENT, tool_name="gmail_search"))
    assert chip is not None
    assert chip.kind == "tool_call"
    assert chip.status == "running"
    assert "gmail_search" in chip.label


def test_tool_call_finished_ok_and_error() -> None:
    ok = project(ToolCallFinished(agent_ref=AGENT, tool_name="gmail_search", ok=True))
    assert ok is not None and ok.kind == "tool_call" and ok.status == "ok"

    err = project(
        ToolCallFinished(
            agent_ref=AGENT, tool_name="gmail_search", ok=False, error_code="invalid_query"
        )
    )
    assert err is not None
    assert err.kind == "tool_call"
    assert err.status == "error"
    assert "invalid_query" in err.label


def test_ask_user_is_info_chip() -> None:
    chip = project(AskUser(agent_ref=AGENT, question="Quel dossier ?"))
    assert chip is not None
    assert chip.kind == "ask_user"
    assert chip.status == "info"
    assert "Quel dossier" in chip.label


def test_stall_nudge_and_force_are_warn() -> None:
    nudge = project(StallNudge(agent_ref=AGENT, forced=False))
    assert nudge is not None and nudge.kind == "stall" and nudge.status == "warn"

    force = project(StallNudge(agent_ref=AGENT, forced=True))
    assert force is not None and force.kind == "stall" and force.status == "warn"
    # The forced variant says something distinct so the user sees the escalation.
    assert force.label != nudge.label


def test_cap_kinds_map_to_distinct_warn_labels() -> None:
    labels = set()
    for cap in ("iteration", "wall_clock", "token"):
        chip = project(CapReached(agent_ref=AGENT, cap=cap))
        assert chip is not None
        assert chip.kind == "cap"
        assert chip.status == "warn"
        labels.add(chip.label)
    assert len(labels) == 3


def test_retry_is_warn_with_attempt() -> None:
    chip = project(Retry(agent_ref=AGENT, attempt=2, error_code="invalid_output"))
    assert chip is not None
    assert chip.kind == "retry"
    assert chip.status == "warn"
    assert "2" in chip.label


def test_validation_failed_is_error_chip() -> None:
    chip = project(Validation(agent_ref=AGENT, ok=False, what="livrable", detail="props invalides"))
    assert chip is not None
    assert chip.kind == "validation_failed"
    assert chip.status == "error"


def test_to_wire_shape() -> None:
    chip = AgentActivity(agent_ref=AGENT, kind="tool_call", label="Outil x", status="ok")
    assert chip.to_wire() == {
        "type": "agent_activity",
        "agent_ref": AGENT,
        "kind": "tool_call",
        "label": "Outil x",
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# 2. Validation aggregation — a PASS produces no chip
# ---------------------------------------------------------------------------


def test_passing_validation_is_suppressed() -> None:
    """A passing validation must NOT each produce a chip (PRD 0011 aggregation)."""

    assert project(Validation(agent_ref=AGENT, ok=True, what="livrable")) is None
    assert project(Validation(agent_ref=AGENT, ok=True, what="arguments de gmail_search")) is None


def test_failing_validation_still_surfaces() -> None:
    """The salient rejection is never suppressed."""

    chip = project(Validation(agent_ref=AGENT, ok=False, what="arguments de gmail_search"))
    assert chip is not None
    assert chip.kind == "validation_failed"
    assert chip.status == "error"


# ---------------------------------------------------------------------------
# 3. Redaction — no Mail subject / snippet leaks onto the user-facing channel
# ---------------------------------------------------------------------------


def test_ask_user_question_is_truncated_no_full_body_leak() -> None:
    """A long ``ask_user`` question echoing an email body is hard-truncated.

    The chip is a one-line metadata marker — the full text travels on the task
    message channel, never on the chip, so even if the model echoed a subject
    into the question, the chip cannot carry the whole body.
    """

    leaky = "Objet : Salaire confidentiel 50000 EUR " + ("x" * 200)
    chip = project(AskUser(agent_ref=AGENT, question=leaky))
    assert chip is not None
    # Hard length bound (label = "Question : " + ≤80 redacted chars + "…").
    assert len(chip.label) < 120
    assert chip.label.endswith("…")
    # No newline / multi-line body smuggled in.
    assert "\n" not in chip.label


def test_validation_detail_is_redacted_and_bounded() -> None:
    detail = "subject='Top secret merger' " + ("y" * 300)
    chip = project(Validation(agent_ref=AGENT, ok=False, what="livrable", detail=detail))
    assert chip is not None
    assert "\n" not in chip.label
    # The detail fragment is collapsed + truncated, never the full 300-char run.
    assert len(chip.label) < 200


def test_redact_payload_scrubs_mail_descriptor() -> None:
    """The shared Mail-field boundary (issue 0056) is reapplied on this channel."""

    original_props: dict[str, Any] = {
        "subject": "Salaire confidentiel",
        "bodyPreview": "Voici votre fiche de paie",
        "snippet": "secret",
        "from": {"name": "RH", "email": "rh@x.com"},
        "messageId": "m1",
    }
    payload: dict[str, Any] = {"component": "Mail", "props": original_props}
    scrubbed = redact_payload(payload)
    assert isinstance(scrubbed, dict)
    props: dict[str, Any] = scrubbed["props"]
    assert props["subject"] != "Salaire confidentiel"
    assert props["bodyPreview"] != "Voici votre fiche de paie"
    assert props["snippet"] != "secret"
    # Metadata survives.
    assert props["messageId"] == "m1"
    assert props["from"] == {"name": "RH", "email": "rh@x.com"}
    # The original object is not mutated.
    assert original_props["subject"] == "Salaire confidentiel"


def test_redact_payload_passes_non_mail_through() -> None:
    md = {"component": "Markdown", "props": {"content": "# hi"}}
    assert redact_payload(md) == md
    assert redact_payload("plain string") == "plain string"
    assert redact_payload(None) is None
