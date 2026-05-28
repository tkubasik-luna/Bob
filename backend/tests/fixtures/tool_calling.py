"""Golden tool-calling fixtures — locks today's behaviour (PRD 0008 / issue 0057).

This module is the single source of truth for the *current* tool-calling
behaviour across the three divergent parse paths Bob runs today. It is pure
data (raw model-output strings + the parsed result the current code produces,
or the failure it currently raises) so every later phase of the 0008
tool-calling unification refactor can re-import the same fixtures and prove it
did not regress — and, where a phase deliberately changes behaviour (e.g. the
guided-JSON phase teaching the sub-agent to accept prose-wrapped envelopes),
flip exactly one assertion against a named fixture.

The three paths, and where each is asserted:

1. **Jarvis + LM Studio native** — ``LMStudioClient.complete`` reads
   ``message.tool_calls`` and ``json.loads`` the ``function.arguments`` string.
   Asserted in ``tests/test_llm_client.py``.
2. **Jarvis + Claude CLI prompt-based** — ``ClaudeCliClient.complete`` asks the
   model for ``{"tool_calls":[…]}`` in the system prompt, then parses with
   ``raw_decode`` + a brace-repair salvage pass (``_repair_json_braces``).
   Asserted in ``tests/test_llm_client.py``.
3. **Sub-agent action envelope** — ``runner._normalise_payload`` strips a code
   fence then ``json.loads`` the ``{"action":…}`` envelope and validates it via
   ``actions.parse_action``. Asserted in ``tests/test_sub_agent_v2_runner.py``.

Each fixture is a frozen dataclass carrying a stable ``id`` so failures name the
exact case. ``MalformedRepairFixture`` additionally records what the salvage
pass currently recovers; ``EnvelopeFixture.parses`` records whether the
sub-agent envelope path accepts the raw string TODAY.

IMPORTANT current-behaviour notes captured here (verified against the code, not
assumed — the issue text predates a couple of these):

- The sub-agent path *does* strip a leading ```` ```json ```` / bare ```` ``` ````
  fence before ``json.loads`` (``_normalise_payload`` calls ``_strip_code_fence``
  first), so a cleanly-fenced envelope **parses** today. The live failure mode
  that actually reproduces is a **prose-wrapped** envelope (prefix or trailing
  prose) and a fenced envelope with trailing prose *after* the closing fence —
  ``_strip_code_fence`` only strips a fence whose last line is the closer, so
  trailing prose defeats it and ``json.loads`` raises.
- ``_strip_code_fence`` leaves a non-``json`` language tag (e.g. ```` ```python ````)
  untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NativeToolCallFixture:
    """A well-formed LM Studio native tool call.

    ``arguments_raw`` is the JSON *string* the provider puts on
    ``function.arguments`` (LM Studio always serialises arguments to a string);
    ``expected_name`` / ``expected_arguments`` are what
    ``LMStudioClient.complete`` decodes them into.
    """

    id: str
    name: str
    arguments_raw: str
    expected_name: str
    expected_arguments: dict[str, Any]


@dataclass(frozen=True)
class ClaudeToolCallFixture:
    """A Claude-CLI prompt-based tool-call reply (well-formed or prose-trailing).

    ``raw`` is the full string the CLI returns; ``expected_calls`` is the list
    of ``(name, arguments)`` tuples ``ClaudeCliClient.complete`` parses out.
    """

    id: str
    raw: str
    expected_calls: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class MalformedRepairFixture:
    """A broken-brace tool-call string the Claude CLI salvage pass repairs.

    ``raw`` is the structurally-broken string the model emitted.
    ``repairs_to_valid_json`` records whether ``_repair_json_braces`` currently
    returns a string that ``json.loads`` accepts (``None`` return → unrepairable).
    ``expected_arguments`` (when repairable) is the ``arguments`` dict the
    repaired-then-parsed first tool call yields.
    """

    id: str
    raw: str
    repairs_to_valid_json: bool
    expected_arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class EnvelopeFixture:
    """A sub-agent action envelope string + whether the runner parses it today.

    ``raw`` is the string the sub-agent LLM emitted. ``parses`` is the current
    behaviour of ``runner._normalise_payload``: ``True`` → it decodes + validates
    into an action; ``False`` → it raises ``SubAgentActionParseError`` (which the
    runner turns into ``done(failed, invalid_output)``). When ``parses`` is True,
    ``expected_action`` / ``expected_fields`` describe the parsed action.
    """

    id: str
    raw: str
    parses: bool
    expected_action: str | None = None
    expected_fields: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path 1 — Jarvis + LM Studio native (message.tool_calls)
# ---------------------------------------------------------------------------

#: Well-formed native calls. LM Studio hands back ``function.arguments`` as a
#: JSON string; ``complete`` ``json.loads`` it. Empty-string arguments decode to
#: ``{}`` (the ``arguments_raw if arguments_raw else {}`` branch).
NATIVE_WELL_FORMED: tuple[NativeToolCallFixture, ...] = (
    NativeToolCallFixture(
        id="native/simple-args",
        name="spawn_subtask",
        arguments_raw='{"title": "buy milk"}',
        expected_name="spawn_subtask",
        expected_arguments={"title": "buy milk"},
    ),
    NativeToolCallFixture(
        id="native/nested-args",
        name="say",
        arguments_raw=json.dumps(
            {
                "speech": "Bitcoin, en bref",
                "ui": {"component": "Markdown", "props": {"content": "rare et cher"}},
            }
        ),
        expected_name="say",
        expected_arguments={
            "speech": "Bitcoin, en bref",
            "ui": {"component": "Markdown", "props": {"content": "rare et cher"}},
        },
    ),
    NativeToolCallFixture(
        id="native/empty-args-string",
        name="spawn_subtask",
        arguments_raw="",
        expected_name="spawn_subtask",
        expected_arguments={},
    ),
)

#: Malformed native arguments. A non-JSON ``function.arguments`` string makes
#: ``complete`` raise ``LLMClientError`` ("not valid JSON"). The native path has
#: NO brace-repair salvage — unlike the Claude CLI path — so this is a hard fail.
NATIVE_MALFORMED_ARGUMENTS_RAW: str = "this-is-not-json"


# ---------------------------------------------------------------------------
# Path 2 — Jarvis + Claude CLI prompt-based ({"tool_calls":[…]})
# ---------------------------------------------------------------------------

#: Well-formed Claude-CLI replies. The second carries trailing prose after the
#: JSON object — ``raw_decode`` recognises the leading object and the prose is
#: ignored (a documented real-world shape: the model confirms in natural
#: language after emitting the call).
CLAUDE_WELL_FORMED: tuple[ClaudeToolCallFixture, ...] = (
    ClaudeToolCallFixture(
        id="claude/clean",
        raw=json.dumps(
            {
                "tool_calls": [
                    {"id": "call_99", "name": "spawn_subtask", "arguments": {"title": "buy milk"}}
                ]
            }
        ),
        expected_calls=(("spawn_subtask", {"title": "buy milk"}),),
    ),
    ClaudeToolCallFixture(
        id="claude/trailing-prose",
        raw=(
            '{"tool_calls": [{"id": "call_1", "name": "spawn_subtask", '
            '"arguments": {"title": "Draft email", "goal": "Write three variants."}}]}'
            "\n\nTâche lancée. Résultat dans quelques instants."
        ),
        expected_calls=(
            ("spawn_subtask", {"title": "Draft email", "goal": "Write three variants."}),
        ),
    ),
)

#: A well-formed Claude reply wrapped in a ```` ```json ```` fence. The CLI path
#: calls ``_strip_code_fence`` before parsing, so the fence is removed and the
#: call parses. (Contrast with the sub-agent path, which also strips a clean
#: fence — see ``ENVELOPE_FIXTURES``.)
CLAUDE_FENCED: ClaudeToolCallFixture = ClaudeToolCallFixture(
    id="claude/fenced-json",
    raw=('```json\n{"tool_calls": [{"id": "c1", "name": "spawn_subtask", "arguments": {}}]}\n```'),
    expected_calls=(("spawn_subtask", {}),),
)

#: Broken-brace cases the Claude CLI salvage pass (``_repair_json_braces``)
#: handles. Each is a string ``raw_decode`` rejects; the repair pass rebuilds the
#: closers from the open-stack. The ``unrepairable`` case has no opener so the
#: repair returns ``None`` and the call is dropped (response falls back to text).
CLAUDE_MALFORMED_REPAIR: tuple[MalformedRepairFixture, ...] = (
    MalformedRepairFixture(
        # Observed in production: a ``say`` with a nested ``ui.props.content``
        # block; the model emitted ``}}}}}]`` where ``}}}}]}`` was needed.
        id="repair/extra-brace-wrong-closer",
        raw=(
            '{"tool_calls": [{"id": "call_1", "name": "say", "arguments": '
            '{"speech": "Bitcoin, en bref", "ui": {"component": "Markdown", '
            '"props": {"content": "rare et cher"}}}}}]'
        ),
        repairs_to_valid_json=True,
        expected_arguments={
            "speech": "Bitcoin, en bref",
            "ui": {"component": "Markdown", "props": {"content": "rare et cher"}},
        },
    ),
    MalformedRepairFixture(
        # Truncated tail: the model stopped emitting before closing the
        # containers. The repair closes everything still open at EOF.
        id="repair/truncated-tail",
        raw='{"tool_calls": [{"name": "say", "arguments": {"speech": "hi"',
        repairs_to_valid_json=True,
        expected_arguments={"speech": "hi"},
    ),
    MalformedRepairFixture(
        # Braces inside a string literal must NOT be counted as structure, and
        # trailing extra braces are trimmed once the top-level value balances.
        id="repair/braces-inside-strings",
        raw='{"a": "x}y{z", "b": 1}}}',
        repairs_to_valid_json=True,
        expected_arguments=None,  # not a tool_calls payload; asserted via _repair only
    ),
    MalformedRepairFixture(
        # No opener at all → ``_repair_json_braces`` returns ``None`` → the CLI
        # path treats the reply as plain text (no tool call salvaged).
        id="repair/unrepairable-prose",
        raw="just prose, no json",
        repairs_to_valid_json=False,
        expected_arguments=None,
    ),
)


# ---------------------------------------------------------------------------
# Path 3 — Sub-agent action envelope ({"action":"tool_call",…})
# ---------------------------------------------------------------------------


def _envelope(action: dict[str, Any]) -> str:
    return json.dumps(action)


def _fenced(action: dict[str, Any], *, lang: str = "json") -> str:
    return f"```{lang}\n{_envelope(action)}\n```"


_TOOL_CALL: dict[str, Any] = {"action": "tool_call", "name": "web_search", "args": {"query": "x"}}
_PROGRESS: dict[str, Any] = {"action": "progress", "thought": "thinking about the goal"}
_DONE: dict[str, Any] = {
    "action": "done",
    "result_summary": "all done",
    "ui_payload": None,
    "status": "complete",
    "reason_code": "ok",
    "cost": {},
}

#: Sub-agent envelope fixtures and the CURRENT ``_normalise_payload`` behaviour.
#:
#: Well-formed + cleanly-fenced envelopes parse. Prose-wrapped (prefix OR
#: trailing) envelopes and a fenced envelope with trailing prose AFTER the close
#: fence do NOT parse today — ``_strip_code_fence`` only strips a fence whose last
#: line is the closer, and ``json.loads`` then chokes on the surrounding prose,
#: raising ``SubAgentActionParseError`` (→ runner forces
#: ``done(failed, invalid_output)``). A non-``json`` fence language is also left
#: untouched and fails to parse. The guided-JSON phase (issue 0060) is expected
#: to flip the ``parses=False`` cases to ``True``.
ENVELOPE_FIXTURES: tuple[EnvelopeFixture, ...] = (
    EnvelopeFixture(
        id="envelope/tool-call-clean",
        raw=_envelope(_TOOL_CALL),
        parses=True,
        expected_action="tool_call",
        expected_fields={"name": "web_search", "args": {"query": "x"}},
    ),
    EnvelopeFixture(
        id="envelope/progress-clean",
        raw=_envelope(_PROGRESS),
        parses=True,
        expected_action="progress",
        expected_fields={"thought": "thinking about the goal"},
    ),
    EnvelopeFixture(
        id="envelope/done-clean",
        raw=_envelope(_DONE),
        parses=True,
        expected_action="done",
        expected_fields={"status": "complete", "reason_code": "ok"},
    ),
    EnvelopeFixture(
        # Cleanly fenced → stripped → parses TODAY. (Issue text predates the
        # fence-strip in _normalise_payload; this asserts the real behaviour.)
        id="envelope/tool-call-fenced-json",
        raw=_fenced(_TOOL_CALL),
        parses=True,
        expected_action="tool_call",
        expected_fields={"name": "web_search", "args": {"query": "x"}},
    ),
    EnvelopeFixture(
        id="envelope/progress-fenced-bare",
        raw=_fenced(_PROGRESS, lang=""),
        parses=True,
        expected_action="progress",
        expected_fields={"thought": "thinking about the goal"},
    ),
    EnvelopeFixture(
        # Prose BEFORE the JSON → json.loads fails → does NOT parse today.
        id="envelope/prose-prefix",
        raw="Here is my next action:\n" + _envelope(_PROGRESS),
        parses=False,
    ),
    EnvelopeFixture(
        # Prose AFTER the JSON → json.loads "Extra data" → does NOT parse today.
        id="envelope/prose-suffix",
        raw=_envelope(_PROGRESS) + "\nLet me know if that helps.",
        parses=False,
    ),
    EnvelopeFixture(
        # Fence WITH trailing prose after the close fence: _strip_code_fence
        # can't strip it (last line isn't the closer) → does NOT parse today.
        id="envelope/fenced-trailing-prose",
        raw=_fenced(_PROGRESS) + "\nDone thinking.",
        parses=False,
    ),
    EnvelopeFixture(
        # Non-``json`` fence language is left untouched by _strip_code_fence →
        # the leading ``` defeats json.loads → does NOT parse today.
        id="envelope/fenced-wrong-lang",
        raw=_fenced(_TOOL_CALL, lang="python"),
        parses=False,
    ),
)
