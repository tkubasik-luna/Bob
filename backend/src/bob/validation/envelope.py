"""Transient :class:`CallEnvelope` carrying the retry counter (PRD 0006 / issue 0048).

The envelope is intentionally **NOT** persisted anywhere — it lives only
for the duration of one tool-call attempt and dies at the end of the
:class:`bob.orchestrator.Orchestrator.process_user_message` turn (or one
sub-agent iteration). Keeping the counter off :class:`ContextEntry` rows
is a load-bearing constraint of the issue: a retried call must leave no
forensic trail in the persisted history.

The envelope also stores the running list of "feedback strings" that the
dispatcher re-injects into the next LLM call under the ``system_validator``
role. The feedback is the *escaped* description of why the previous
attempt failed; the offending raw output never lands in this list — the
escape step happens at the call site in :mod:`bob.validation.system_validator`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CallEnvelope:
    """Per-call retry bookkeeping.

    Created freshly at the top of each Jarvis turn (or sub-agent iteration)
    and dropped on exit. Mutating in-place is fine because the envelope
    never escapes the turn that owns it.

    Fields:

    - ``tool_name`` — the LLM-facing tool name the envelope is tracking
      (``"say"``, ``"spawn_subtask"``…). ``None`` when the envelope is
      bound to a generic actor (the sub-agent runner uses
      ``actor="sub_agent"`` instead).
    - ``actor`` — ``"jarvis"`` or ``"sub_agent"``. Used by
      :mod:`bob.validation.exhausted` to dispatch to the right
      ``on_validation_exhausted`` handler.
    - ``attempts`` — number of attempts performed so far (1 on the first
      try, 2 after the first retry, …). ``increment`` bumps it.
    - ``feedback`` — escaped feedback strings re-injected by the
      dispatcher under the ``system_validator`` role on every retry.
    - ``last_error_code`` — last seen :class:`bob.validation.reason_codes.ReasonCode`
      shorthand; surfaced in the structured log when the budget is
      exhausted.
    """

    tool_name: str | None
    actor: str
    attempts: int = 1
    feedback: list[str] = field(default_factory=list)
    last_error_code: str | None = None

    def increment(self, *, error_code: str | None = None) -> None:
        """Record a failed attempt + bump the counter."""

        self.attempts += 1
        if error_code is not None:
            self.last_error_code = error_code

    def record_feedback(self, escaped_feedback: str) -> None:
        """Append a feedback line (already escaped — call site enforces)."""

        self.feedback.append(escaped_feedback)

    @property
    def retries_used(self) -> int:
        """Number of retries performed (``attempts - 1``)."""

        return max(0, self.attempts - 1)


__all__ = ["CallEnvelope"]
