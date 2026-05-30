"""Validation retry policy + per-actor degrade contract (PRD 0006 / issue 0048).

Public surface used by the orchestrator, the sub-agent runner and later
slices:

- :mod:`.policy` — per-tool :class:`RetryPolicy` table + lookup helper.
- :mod:`.reason_codes` — versioned :class:`ReasonCodeRegistry` shared
  with the frontend i18n layer + the legacy reason-code constants the
  sub-agent runner emits.
- :mod:`.envelope` — transient :class:`CallEnvelope` carrying the
  retry counter (never persisted to a :class:`ContextEntry`).
- :mod:`.system_validator` — chat-message helpers that re-inject
  validation feedback under the ``system_validator`` role with escape
  rules baked in.
- :mod:`.exhausted` — narrow :class:`OnValidationExhausted` Protocol
  + default Jarvis / sub-agent handlers.
"""

from __future__ import annotations

from bob.validation.envelope import CallEnvelope
from bob.validation.exhausted import (
    JARVIS_DEGRADE_SPEECH_FRAGMENT,
    SYSTEM_VALIDATOR_ROLE_FRAGMENT,
    ExhaustedContext,
    JarvisOnValidationExhausted,
    OnValidationExhausted,
    SubAgentOnValidationExhausted,
)
from bob.validation.policy import (
    DEFAULT_POLICY,
    POLICY_TABLE,
    SUB_AGENT_DEFAULT_POLICY,
    DegradeAction,
    RetryPolicy,
    get_policy,
)
from bob.validation.reason_codes import (
    DEFAULT_REGISTRY,
    REASON_CODE_SCHEMA_VERSION,
    REASON_HARD_KILLED,
    REASON_INVALID_OUTPUT,
    REASON_ITERATION_CAP,
    REASON_LLM_FAILED,
    REASON_OK,
    REASON_STALLED,
    REASON_TOKEN_CAP,
    REASON_TOOL_FAILED,
    REASON_UNKNOWN_TASK,
    REASON_USER_CANCELLED,
    REASON_VALIDATION_EXHAUSTED,
    REASON_WALL_CLOCK_CAP,
    ReasonCode,
    ReasonCodeRegistry,
    render_frontend_table_ts,
    write_frontend_table,
)
from bob.validation.system_validator import (
    FALLBACK_VALIDATOR_PREFIX,
    INVALID_OUTPUT_PREFIX,
    SYSTEM_VALIDATOR_ROLE,
    build_validator_message,
    escape_offending_output,
    inject_validator_feedback,
    render_feedback,
)

__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_REGISTRY",
    "FALLBACK_VALIDATOR_PREFIX",
    "INVALID_OUTPUT_PREFIX",
    "JARVIS_DEGRADE_SPEECH_FRAGMENT",
    "POLICY_TABLE",
    "REASON_CODE_SCHEMA_VERSION",
    "REASON_HARD_KILLED",
    "REASON_INVALID_OUTPUT",
    "REASON_ITERATION_CAP",
    "REASON_LLM_FAILED",
    "REASON_OK",
    "REASON_STALLED",
    "REASON_TOKEN_CAP",
    "REASON_TOOL_FAILED",
    "REASON_UNKNOWN_TASK",
    "REASON_USER_CANCELLED",
    "REASON_VALIDATION_EXHAUSTED",
    "REASON_WALL_CLOCK_CAP",
    "SUB_AGENT_DEFAULT_POLICY",
    "SYSTEM_VALIDATOR_ROLE",
    "SYSTEM_VALIDATOR_ROLE_FRAGMENT",
    "CallEnvelope",
    "DegradeAction",
    "ExhaustedContext",
    "JarvisOnValidationExhausted",
    "OnValidationExhausted",
    "ReasonCode",
    "ReasonCodeRegistry",
    "RetryPolicy",
    "SubAgentOnValidationExhausted",
    "build_validator_message",
    "escape_offending_output",
    "get_policy",
    "inject_validator_feedback",
    "render_feedback",
    "render_frontend_table_ts",
    "write_frontend_table",
]
