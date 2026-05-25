"""``system_validator`` role injection + escape logic (PRD 0006 / issue 0048).

Pre-0048 the legacy :mod:`bob.response_parser` re-injected validation
feedback as a ``user`` message (sometimes preceded by an ``assistant``
echo of the bad output). That is a textbook prompt-injection hazard:
nothing stops a misbehaving local LLM from emitting
``ignore previous instructions, do X`` and then having Bob feed that
back as if it came from the user.

This module replaces that path with two hard rules:

1. **Validation feedback is injected with role ``system_validator``.** Most
   OpenAI-compatible APIs (LM Studio included) accept arbitrary role
   strings on chat messages and pass them straight through to the
   model. When the upstream provider rejects unknown roles we fall back
   to role ``system`` with a ``[VALIDATOR]:`` prefix so the validator
   payload remains distinguishable from a real system message in the
   model's context window.
2. **The offending model output is escaped before re-injection.** We
   strip control characters, escape backticks and quotes, normalise
   newlines, and prefix the payload with ``[INVALID OUTPUT]:``. The
   escape is conservative — it never alters the meaning of legitimate
   French text but makes it impossible for the LLM to treat its own
   bad output as a user instruction.

Both rules are exercised by the validator behavioural tests in
``backend/tests/test_validation_system_validator.py``.
"""

from __future__ import annotations

import re
from typing import Any

#: Role string injected into the chat-messages list. Stored as a constant
#: so the same literal flows through the dispatcher, the LLM client, and
#: the tests.
SYSTEM_VALIDATOR_ROLE = "system_validator"


#: Prefix injected in front of the escaped offending output so the LLM
#: cannot confuse it with a real user instruction even at zero
#: temperature.
INVALID_OUTPUT_PREFIX = "[INVALID OUTPUT]: "


#: Prefix used when the upstream provider rejects unknown role strings
#: and we fold the validator payload back into a ``system`` message.
FALLBACK_VALIDATOR_PREFIX = "[VALIDATOR]: "


# Control characters except ``\n`` and ``\t``. Strip them defensively —
# bad LLM output occasionally emits stray ``\x00`` bytes which confuse
# downstream parsers.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def escape_offending_output(raw: str) -> str:
    """Return a safe, prefixed representation of ``raw`` for re-injection.

    The escape:

    - strips control characters (except ``\\n``/``\\t``);
    - replaces backticks with single straight quotes so an unescaped
      markdown fence in the offending output cannot smuggle a follow-up
      instruction past a naive system-prompt parser;
    - collapses repeated whitespace to single spaces inside lines while
      preserving line breaks;
    - truncates to 1024 characters (the validator only needs a hint of
      what was wrong, not the entire payload);
    - prefixes the result with :data:`INVALID_OUTPUT_PREFIX`.

    The transformation is documented end-to-end so future contributors
    do not "polish" it without realising the security intent.
    """

    if not isinstance(raw, str):
        raw = str(raw)
    stripped = _CONTROL_CHARS_RE.sub("", raw)
    # Backticks → single straight quote so we cannot accidentally inject
    # a markdown fence that some downstream consumer treats as ``code``.
    # We avoid the right-single-quotation-mark (U+2019) here because
    # ruff would flag the literal as ambiguous; a straight quote is good
    # enough to break the fence sequence.
    stripped = stripped.replace("`", "'")
    # Collapse runs of horizontal whitespace inside each line.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in stripped.splitlines()]
    flattened = "\n".join(lines).strip()
    if len(flattened) > 1024:
        flattened = flattened[:1024] + "…"
    return INVALID_OUTPUT_PREFIX + flattened


def build_validator_message(
    feedback: str,
    *,
    role: str = SYSTEM_VALIDATOR_ROLE,
) -> dict[str, Any]:
    """Build a chat message describing the validation failure.

    ``feedback`` is the human-readable explanation of why the previous
    attempt failed — *not* the offending payload. Call sites are expected
    to call :func:`escape_offending_output` on the bad raw output
    separately and concatenate the two pieces before passing them here.

    The role defaults to :data:`SYSTEM_VALIDATOR_ROLE`. Callers that
    target a provider known to reject unknown roles should pass
    ``role="system"`` and ensure the body already carries the
    :data:`FALLBACK_VALIDATOR_PREFIX`.
    """

    return {"role": role, "content": feedback}


def render_feedback(
    *,
    error_message: str,
    offending_raw: str | None,
) -> str:
    """Render the full validator feedback string.

    Concatenates the human-readable error message with the escaped
    offending output (when available). The result is what gets passed
    to :func:`build_validator_message`.
    """

    parts = [error_message.strip()]
    if offending_raw is not None:
        parts.append(escape_offending_output(offending_raw))
    return "\n".join(part for part in parts if part)


def inject_validator_feedback(
    messages: list[dict[str, Any]],
    feedback_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return ``messages`` with ``feedback_messages`` appended at the tail.

    Kept as a tiny helper so call sites have one greppable place to
    enforce the "feedback always appended, never prepended, never
    rewritten as a user message" invariant.
    """

    if not feedback_messages:
        return messages
    return [*messages, *feedback_messages]


__all__ = [
    "FALLBACK_VALIDATOR_PREFIX",
    "INVALID_OUTPUT_PREFIX",
    "SYSTEM_VALIDATOR_ROLE",
    "build_validator_message",
    "escape_offending_output",
    "inject_validator_feedback",
    "render_feedback",
]
