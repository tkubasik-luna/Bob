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
   Asserted in ``tests/test_llm_client.py``. UNCHANGED across all 0008 phases.
2. **Jarvis + Claude CLI Hermes tags** — ``ClaudeCliClient.complete`` advertises
   the tools as a ``<tools>`` block (Nous-Hermes ChatML) and parses the model's
   ``<tool_call>{…}</tool_call>`` replies through the
   :class:`bob.llm.tooling.hermes.HermesToolCodec` tolerant chain
   (``json → ast.literal_eval → fenced-JSON``, NO brace counting).
   Asserted in ``tests/test_llm_client.py``.

   **Issue 0061 changed this path.** Before 0061 the CLI hand-wrote a
   ``{"tool_calls":[…]}`` blob and salvaged miscounted braces with
   ``_repair_json_braces``; that fragile wire format + the brace-repair
   primitive are deleted. The fixtures below now describe the Hermes wire
   format and the tolerant-chain recovery. The old top-level "broken-brace"
   inputs are no longer *produced* by Hermes, so they are not reproduced as
   recoverable cases — a garbled span the chain cannot decode degrades to text
   (``[]``); bounded-retry-with-error-echo is the self-correction loop's job
   (issue 0062), not the codec's.
3. **Sub-agent action envelope** — ``runner._normalise_payload`` strips a code
   fence then ``json.loads`` the ``{"action":…}`` envelope and validates it via
   ``actions.parse_action``. Asserted in ``tests/test_sub_agent_v2_runner.py``.
   UNCHANGED by issue 0061.

Each fixture is a frozen dataclass carrying a stable ``id`` so failures name the
exact case. ``HermesToleranceFixture`` records, for one ``<tool_call>`` reply,
the calls the tolerant chain decodes (empty tuple → degrades to text);
``EnvelopeFixture.parses`` records whether the sub-agent envelope path accepts
the raw string TODAY.

IMPORTANT current-behaviour notes captured here (verified against the code, not
assumed):

- The Hermes path wraps the reply in ``<root>`` and extracts every
  ``<tool_call>`` span (real XML parse, regex fallback when the JSON body has
  XML-illegal ``&`` / ``<`` chars), decoding each via ``json → ast.literal_eval
  → fenced-JSON``. Prose around the tags, single-quoted Python dicts, fenced
  bodies, and XML-illegal characters all recover; a reply with no
  ``<tool_call>`` tag (or a body none of the rungs decode) yields ``[]``.
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
class HermesToolCallFixture:
    """A well-formed Claude-CLI Hermes ``<tool_call>`` reply.

    ``raw`` is the full string the CLI returns (one or more ``<tool_call>``
    blocks, optionally wrapped in prose / a fence); ``expected_calls`` is the
    list of ``(name, arguments)`` tuples the
    :class:`bob.llm.tooling.hermes.HermesToolCodec` parses out, in order.
    """

    id: str
    raw: str
    expected_calls: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class HermesToleranceFixture:
    """A garbled / non-strict ``<tool_call>`` reply + what the chain recovers.

    ``raw`` is the string the model emitted. ``expected_calls`` is what the
    tolerant chain (``json → ast.literal_eval → fenced-JSON``) decodes — an
    **empty tuple** means the reply degrades to plain text (the Hermes path
    yields no calls; recovery of a still-malformed call is issue 0062's
    self-correction loop, not the codec). This replaces the old
    ``MalformedRepairFixture`` (the brace-repair primitive is deleted in 0061).
    """

    id: str
    raw: str
    expected_calls: tuple[tuple[str, dict[str, Any]], ...]


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
# Path 2 — Jarvis + Claude CLI Hermes tags (<tool_call>{…}</tool_call>)
# ---------------------------------------------------------------------------


def _tool_call_tag(name: str, arguments: dict[str, Any], *, call_id: str | None = None) -> str:
    """Wrap a call as a Nous-Hermes ``<tool_call>{…}</tool_call>`` block."""

    body: dict[str, Any] = {"name": name, "arguments": arguments}
    if call_id is not None:
        body = {"id": call_id, **body}
    return f"<tool_call>{json.dumps(body, ensure_ascii=False)}</tool_call>"


#: Well-formed Hermes ``<tool_call>`` replies the codec parses byte-for-byte.
#: ``hermes/trailing-prose`` carries a confirmation sentence after the block
#: (the model often narrates after the call) — the ``<root>`` wrap + span
#: extraction ignore the surrounding prose. ``hermes/multiple`` emits two
#: blocks (Hermes allows several calls per turn). ``hermes/nested-args`` is the
#: deeply-nested ``say`` shape that used to trip the brace-repair pass — under
#: Hermes it is just well-formed JSON inside the tags.
CLAUDE_WELL_FORMED: tuple[HermesToolCallFixture, ...] = (
    HermesToolCallFixture(
        id="hermes/clean",
        raw=_tool_call_tag("spawn_subtask", {"title": "buy milk"}, call_id="call_99"),
        expected_calls=(("spawn_subtask", {"title": "buy milk"}),),
    ),
    HermesToolCallFixture(
        id="hermes/trailing-prose",
        raw=(
            _tool_call_tag(
                "spawn_subtask",
                {"title": "Draft email", "goal": "Write three variants."},
                call_id="call_1",
            )
            + "\n\nTâche lancée. Résultat dans quelques instants."
        ),
        expected_calls=(
            ("spawn_subtask", {"title": "Draft email", "goal": "Write three variants."}),
        ),
    ),
    HermesToolCallFixture(
        id="hermes/multiple",
        raw=(
            _tool_call_tag("spawn_subtask", {"title": "a"})
            + "\n"
            + _tool_call_tag("spawn_subtask", {"title": "b"})
        ),
        expected_calls=(
            ("spawn_subtask", {"title": "a"}),
            ("spawn_subtask", {"title": "b"}),
        ),
    ),
    HermesToolCallFixture(
        id="hermes/nested-args",
        # The old ``repair/extra-brace-wrong-closer`` payload, now well-formed
        # inside <tool_call> tags — Hermes has no brace-counting to break.
        raw=_tool_call_tag(
            "say",
            {
                "speech": "Bitcoin, en bref",
                "ui": {"component": "Markdown", "props": {"content": "rare et cher"}},
            },
        ),
        expected_calls=(
            (
                "say",
                {
                    "speech": "Bitcoin, en bref",
                    "ui": {"component": "Markdown", "props": {"content": "rare et cher"}},
                },
            ),
        ),
    ),
)

#: A Hermes reply whose ``<tool_call>`` body is wrapped in a ```` ```json ````
#: fence. The codec's fence rung (``_strip_code_fence``) unwraps it before
#: decoding. (Contrast the sub-agent path, which strips a clean fence too — see
#: ``ENVELOPE_FIXTURES``.)
CLAUDE_FENCED: HermesToolCallFixture = HermesToolCallFixture(
    id="hermes/fenced-body",
    raw=(
        "<tool_call>\n```json\n"
        + json.dumps({"id": "c1", "name": "spawn_subtask", "arguments": {}})
        + "\n```\n</tool_call>"
    ),
    expected_calls=(("spawn_subtask", {}),),
)

#: Tolerant-chain cases: non-strict ``<tool_call>`` bodies + what the chain
#: recovers (empty tuple → degrades to plain text). These exercise the
#: ``json → ast.literal_eval → fenced-JSON`` ladder and the XML-illegal-char
#: regex fallback that REPLACE the deleted brace-repair primitive.
CLAUDE_TOLERANCE: tuple[HermesToleranceFixture, ...] = (
    HermesToleranceFixture(
        # Single-quoted Python-dict body → recovered via ``ast.literal_eval``.
        id="tolerance/py-dict-single-quotes",
        raw="<tool_call>{'name': 'say', 'arguments': {'speech': 'hi'}}</tool_call>",
        expected_calls=(("say", {"speech": "hi"}),),
    ),
    HermesToleranceFixture(
        # XML-illegal ``&`` / ``<`` in the JSON body → the ``<root>`` XML parse
        # fails and the DOTALL regex fallback extracts the span; the body is
        # still valid JSON so it decodes.
        id="tolerance/xml-illegal-chars",
        raw=(
            '<tool_call>{"name": "say", "arguments": '
            '{"speech": "Tom & Jerry < Batman"}}</tool_call>'
        ),
        expected_calls=(("say", {"speech": "Tom & Jerry < Batman"}),),
    ),
    HermesToleranceFixture(
        # Prose BEFORE the block too (not just after) → span extraction still
        # finds the call regardless of surrounding narration.
        id="tolerance/prose-prefix-and-suffix",
        raw=(
            "Sure, let me do that.\n"
            + _tool_call_tag("spawn_subtask", {"title": "x"})
            + "\nDone — running now."
        ),
        expected_calls=(("spawn_subtask", {"title": "x"}),),
    ),
    HermesToleranceFixture(
        # No ``<tool_call>`` tag at all → plain text → no calls. (The old
        # ``repair/unrepairable-prose`` case; same observable outcome.)
        id="tolerance/no-tags-degrades-to-text",
        raw="just prose, no tool call here",
        expected_calls=(),
    ),
    HermesToleranceFixture(
        # A ``<tool_call>`` whose body none of the rungs can decode (truncated
        # mid-key, not valid JSON or a Python literal) → span skipped → text.
        # Recovery of this is the self-correction loop's job (issue 0062).
        id="tolerance/undecodable-body-degrades-to-text",
        raw='<tool_call>{"name": "say", "arg</tool_call>',
        expected_calls=(),
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
#: Well-formed + cleanly-fenced envelopes parse. A VALID leading JSON object
#: followed by a trailing tail (prose, or a native-tool-use model's hallucinated
#: ``<function_calls>``/``<function_results>`` blocks) ALSO parses now: the runner
#: salvages the leading object via ``raw_decode`` and discards the tail. What
#: still fails: prose BEFORE the JSON, and a fenced envelope ``_strip_code_fence``
#: can't unwrap (trailing prose after the close fence, or a non-``json`` fence
#: language) — the leading ``` defeats ``raw_decode`` → ``SubAgentActionParseError``
#: (→ runner forces ``done(failed, invalid_output)``).
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
        # Prose AFTER a VALID leading JSON object → the envelope is salvaged via
        # ``raw_decode`` and the trailing tail discarded. This is the
        # native-tool-use hallucination fix: sonnet (Claude CLI) emits the valid
        # envelope then keeps generating ``<function_calls>``/``<function_results>``
        # blocks; the leading action must survive. PARSES today.
        id="envelope/prose-suffix",
        raw=_envelope(_PROGRESS) + "\nLet me know if that helps.",
        parses=True,
        expected_action="progress",
        expected_fields={"thought": "thinking about the goal"},
    ),
    EnvelopeFixture(
        # Fence WITH trailing prose after the close fence: ``_strip_code_fence``
        # strips the opening fence line, leaving ``{json}\n```\nDone thinking.`` —
        # a valid leading object + tail, which ``raw_decode`` now salvages.
        # PARSES today (was a fail before the trailing-tolerance fix).
        id="envelope/fenced-trailing-prose",
        raw=_fenced(_PROGRESS) + "\nDone thinking.",
        parses=True,
        expected_action="progress",
        expected_fields={"thought": "thinking about the goal"},
    ),
    EnvelopeFixture(
        # Non-``json`` fence language is left untouched by _strip_code_fence →
        # the leading ``` defeats json.loads → does NOT parse today.
        id="envelope/fenced-wrong-lang",
        raw=_fenced(_TOOL_CALL, lang="python"),
        parses=False,
    ),
)
