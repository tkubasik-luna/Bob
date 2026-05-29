"""Nous-Hermes ``<tool_call>`` codec — the Claude CLI wire format (issue 0061).

PRD 0008 / issue 0061. The Claude CLI has **no native function calling** on the
command line and **no constrained decoding**: robustness has to come entirely
from (a) a prompt format the model was actually trained on and (b) a *tolerant*
parse chain that recovers the common ways a free-running model garbles its
output — without ever hand-rolling brace counting (the fragile thing 0061
deletes).

This codec implements both halves of the :class:`bob.llm.tooling.codec.ToolCodec`
protocol for the Hermes tag format:

- :meth:`inject` advertises the tools as a Nous-Hermes ``<tools> … </tools>``
  ChatML block appended to the system message — one OpenAI-style
  ``{"type": "function", "function": {…}}`` JSON object per line — and states
  the emission protocol (calls wrapped in ``<tool_call>{…}</tool_call>``,
  multiple allowed). It returns ``{}`` request kwargs: the CLI takes no
  per-call tool kwargs, the whole contract lives in the prompt.
- :meth:`parse` reads a whole reply by wrapping it in a synthetic ``<root>``
  element, pulling out every ``<tool_call>`` span (real XML parse first, a
  DOTALL regex fallback when the JSON body contains XML-illegal characters
  such as ``&`` / ``<``), then decoding each span's body through the ladder
  ``json.loads`` (via ``raw_decode`` so trailing prose inside the tags is
  tolerated) → :func:`ast.literal_eval` (single-quoted / Python-dict args) →
  strip a markdown fence and retry. The first step that yields a ``dict``
  carrying a ``name`` wins; an undecodable or nameless span is skipped, so a
  reply with no recoverable call degrades to plain text (``[]``).
- :meth:`stream_parser` hands back a :class:`_HermesStreamParser` that
  re-extracts ``<tool_call>`` spans on every tick for a progressive view and,
  on :meth:`_HermesStreamParser.finish`, also recovers a trailing
  *unterminated-but-decodable* ``<tool_call>`` so a truncated stream still
  resolves. The argument-JSON suffixes flow through as ``tool_call_args_delta``
  chunks byte-for-byte, keeping the ``say`` tool's progressive-TTS
  (:class:`bob.streaming.PartialJsonParser` → ``speech_delta``) working on the
  Claude path.

SECURITY (PRD 0006): Hermes natively feeds tool *results* back as a ``tool``
role; Bob must NOT use the ``tool`` role for error feedback. That is the
self-correction loop's concern (issue 0062) — this codec only owns the wire
format. A malformed call is recovered by the tolerant chain where possible;
the bounded-retry-with-error-echo is explicitly out of scope here.

The codec is stateless and reusable: a future Hermes/vLLM endpoint that speaks
the same tag format can select it through
:func:`bob.llm.tooling.capability.select_codec` unchanged.
"""

from __future__ import annotations

import ast
import json
import re
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4
from xml.etree import ElementTree as ET

from bob.llm.types import StreamChunk, ToolCall

if TYPE_CHECKING:  # pragma: no cover — typing-only import.
    from collections.abc import Iterator

    from bob.llm.tooling.spec import ToolSpec


#: Matches one ``<tool_call> … </tool_call>`` span, body captured. ``DOTALL`` so
#: a multi-line JSON body (a ``say`` with an embedded markdown block) is caught,
#: ``non-greedy`` so adjacent calls don't get merged into one span. This is the
#: fallback used when the wrapped output is not well-formed XML (the model's
#: JSON body routinely contains ``&`` / ``<`` / ``>`` which are illegal in XML
#: text); the happy path goes through :func:`xml.etree.ElementTree` first.
_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)

#: Matches a trailing ``<tool_call>`` whose closing tag never arrived — used
#: only by the streaming ``finish()`` fallback to salvage a truncated final
#: call. Anchored to "no further ``</tool_call>`` after the open tag" via the
#: negative lookahead so it never double-counts an already-closed span.
_UNTERMINATED_TOOL_CALL_RE = re.compile(
    r"<tool_call>(?!.*</tool_call>)(.*)\Z",
    re.DOTALL,
)


def _decode_tool_call_body(body: str) -> dict[str, Any] | None:
    """Decode one ``<tool_call>`` body via the tolerant ladder. No brace counting.

    The ladder, in order — the first rung that yields a ``dict`` wins:

    1. ``json.loads`` via :meth:`json.JSONDecoder.raw_decode` so a leading JSON
       object is recognised even when the model appended prose *inside* the
       tags after the object (the same tolerance the old CLI path had, minus
       the brace-repair hack).
    2. :func:`ast.literal_eval` for a Python-dict / single-quoted-string body
       (``{'name': 'say', 'arguments': {...}}``) — a common shape when a model
       echoes a Python repr instead of strict JSON.
    3. Strip a markdown code fence (reusing the CLI's
       :func:`bob.llm_client._strip_code_fence`) and retry rungs 1 and 2 on the
       unwrapped body.

    Returns the decoded ``dict`` or ``None`` when no rung succeeds (the caller
    skips the span). A decoded non-``dict`` (list, scalar) also yields ``None``
    — only an object is a tool call.
    """

    # Rung 1 — strict-ish JSON, tolerating trailing prose via raw_decode.
    decoded = _try_json(body)
    if decoded is not None:
        return decoded
    # Rung 2 — Python literal (single-quoted keys/values, py dict).
    decoded = _try_ast(body)
    if decoded is not None:
        return decoded
    # Rung 3 — unwrap a markdown fence, then retry rungs 1 and 2.
    # Local import avoids a module-import cycle (``llm_client`` imports the
    # tooling package which would otherwise import it back at module load).
    from bob.llm_client import _strip_code_fence

    unwrapped = _strip_code_fence(body)
    if unwrapped != body:
        decoded = _try_json(unwrapped)
        if decoded is not None:
            return decoded
        decoded = _try_ast(unwrapped)
        if decoded is not None:
            return decoded
    return None


def _try_json(body: str) -> dict[str, Any] | None:
    """``raw_decode`` the body; return the dict or ``None`` (never raises)."""

    stripped = body.strip()
    if not stripped:
        return None
    try:
        payload, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _try_ast(body: str) -> dict[str, Any] | None:
    """``ast.literal_eval`` the body; return the dict or ``None`` (never raises)."""

    stripped = body.strip()
    if not stripped:
        return None
    try:
        payload = ast.literal_eval(stripped)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _to_tool_call(payload: dict[str, Any]) -> ToolCall | None:
    """Project a decoded ``{"name","arguments"}`` dict into a :class:`ToolCall`.

    A nameless payload is not a dispatchable call → ``None`` (the span is
    skipped). ``arguments`` defaults to ``{}`` and a non-object ``arguments``
    is coerced to ``{}`` (defensive — the tolerant chain favours recovering
    *something* over raising; strict re-validation against the tool's schema is
    issue 0062's job, not the codec's). A missing id gets the same
    ``call_<8-hex>`` placeholder the native path uses.
    """

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    raw_id = payload.get("id")
    call_id = raw_id if isinstance(raw_id, str) and raw_id else f"call_{uuid4().hex[:8]}"
    return ToolCall(id=call_id, name=name, arguments=cast(dict[str, Any], arguments))


def _extract_spans(text: str) -> list[str]:
    """Return the body of every ``<tool_call>`` span in ``text``.

    Tries a real XML parse first: wrap the whole reply in a synthetic
    ``<root>`` element so multiple top-level ``<tool_call>`` tags (and the
    surrounding prose, which becomes element ``.text`` / ``.tail``) form one
    well-formed document, then collect each ``<tool_call>`` element's inner
    text. When the wrapped output is *not* well-formed XML — overwhelmingly
    because the JSON body contains a raw ``&`` / ``<`` / ``>`` — fall back to a
    DOTALL regex that pulls each ``<tool_call>…</tool_call>`` span out
    textually. Either way the bodies are handed to the decode ladder unchanged.
    """

    if "<tool_call>" not in text:
        return []
    try:
        root = ET.fromstring(f"<root>{text}</root>")
    except ET.ParseError:
        return [m.group(1) for m in _TOOL_CALL_RE.finditer(text)]
    spans: list[str] = []
    for element in root.iter("tool_call"):
        # ``itertext`` re-joins any child nodes the XML parser split the body
        # into (defensive — a clean JSON body has none, but a stray inner tag
        # must not silently truncate the captured text).
        spans.append("".join(element.itertext()))
    return spans


class HermesToolCodec:
    """Nous-Hermes ``<tool_call>`` tag codec (the Claude CLI path, issue 0061).

    Replaces the deleted bespoke ``{"tool_calls":[…]}`` system addendum +
    ``raw_decode``/``_repair_json_braces`` parse block. ``inject`` speaks the
    trained Hermes prompt format; ``parse`` / ``stream_parser`` decode via the
    tolerant chain in this module. Stateless and reusable for a future
    Hermes/vLLM endpoint.
    """

    def inject(
        self,
        messages: list[dict[str, Any]],
        specs: list[ToolSpec],
    ) -> dict[str, Any]:
        """Append the Hermes ``<tools>`` block to the system message in place.

        Mutates ``messages`` (the prompt is the whole contract for this
        backend): the block is appended to the first ``system`` message, or a
        new ``system`` message is prepended when none exists. One OpenAI-style
        ``{"type": "function", "function": {name, description, parameters}}``
        JSON object per line goes inside ``<tools> … </tools>``, followed by
        the emission protocol. Returns ``{}`` because the CLI takes no per-call
        tool kwargs (contrast the native codec's ``tools=[…]`` block).

        Empty ``specs`` → no block injected and ``{}`` returned (matches the
        old ``if tools:`` guard so a tools-less call is a pure passthrough).
        """

        if not specs:
            return {}

        block = self._build_tools_block(specs)
        for msg in messages:
            if msg.get("role") == "system":
                msg["content"] = str(msg.get("content", "")) + block
                return {}
        messages.insert(0, {"role": "system", "content": block.lstrip()})
        return {}

    @staticmethod
    def _build_tools_block(specs: list[ToolSpec]) -> str:
        """Render the Nous-Hermes ``<tools>`` ChatML block + emission protocol.

        Tool order is preserved (registration order in == lines out);
        deterministic schema ordering is issue 0063's concern, injection order
        is stable here. Each line is a compact one-line JSON object so the
        block stays token-cheap and the trained format is matched exactly.
        """

        lines = [
            json.dumps(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                },
                ensure_ascii=False,
            )
            for spec in specs
        ]
        tools_json = "\n".join(lines)
        return (
            "\n\nYou are a function-calling AI model. You are provided with "
            "function signatures inside <tools></tools> XML tags. You may call "
            "one or more functions to assist with the user query. For each "
            "function call, return a JSON object with the function name and "
            "arguments inside <tool_call></tool_call> XML tags, like:\n"
            '<tool_call>{"name": "<function-name>", "arguments": <args-dict>}</tool_call>\n'
            "Emit one <tool_call> block per call; multiple blocks are allowed. "
            "If you do NOT need a function, reply with plain text and NO "
            "<tool_call> tags.\n"
            f"<tools>\n{tools_json}\n</tools>"
        )

    def parse(self, message: Any) -> list[ToolCall]:
        """Parse a whole Claude-CLI reply string into :class:`ToolCall` objects.

        ``message`` is the raw reply *string* the CLI returned (the core hands
        the codec ``raw``, not an OpenAI message object — there is no native
        ``message.tool_calls`` surface on this backend). Every ``<tool_call>``
        span is extracted (XML first, regex fallback) and decoded via the
        tolerant ladder; spans that don't decode to a named object are skipped.
        An empty result means "no recoverable tool call" → the core falls back
        to treating the reply as plain text.
        """

        text = message if isinstance(message, str) else str(message)
        calls: list[ToolCall] = []
        for span in _extract_spans(text):
            payload = _decode_tool_call_body(span)
            if payload is None:
                continue
            call = _to_tool_call(payload)
            if call is not None:
                calls.append(call)
        return calls

    def stream_parser(self) -> _HermesStreamParser:
        """Return a fresh :class:`_HermesStreamParser` for one stream."""

        return _HermesStreamParser()


class _HermesStreamParser:
    """Per-completion streaming accumulator for the Hermes tag format.

    The Claude CLI itself does not stream tokens (the core replays
    :meth:`HermesToolCodec.parse` output through
    :meth:`bob.llm_client.LLMClient._fallback_stream`), but the protocol
    requires a parser and a future Hermes/vLLM endpoint *will* stream raw
    deltas. This parser accumulates the raw text, re-extracts ``<tool_call>``
    spans each tick to grow a progressive view, and emits the
    ``tool_call_start`` / ``tool_call_args_delta`` / ``tool_call_end``
    lifecycle so :class:`bob.streaming.StreamEmitter` sees the same surface as
    the native path — keeping the ``say`` tool's progressive TTS working.

    Each provider delta is expected to carry plain text on
    ``choices[0].delta.content`` (the Hermes tags + JSON are emitted as content,
    not as a native ``tool_calls`` field). The parser tracks, per resolved span
    index, how much of the JSON argument string it has already surfaced so each
    tick yields only the new suffix (the ``speech_delta`` contract).
    """

    def __init__(self) -> None:
        self._buffer = ""
        #: Per-span-index state: ``{index: {"id","name","arguments_emitted",
        #: "started_yielded","ended"}}``. Span index is positional + stable as
        #: long as earlier ``<tool_call>`` opens don't disappear (they never do
        #: — text only grows).
        self._states: dict[int, dict[str, Any]] = {}

    def feed(self, delta: Any) -> Iterator[StreamChunk]:
        """Consume one ``choices[0].delta`` and yield ready chunks.

        Reads the text content off the delta, appends it to the buffer, then
        re-runs span extraction. For each span that has resolved a ``name`` we
        emit a ``tool_call_start`` once, then a ``tool_call_args_delta`` for the
        newly-grown suffix of that span's serialised ``arguments``. The
        ``tool_call_end`` for a span fires from :meth:`finish` (we only know a
        span is final once the stream closes or its ``</tool_call>`` arrives —
        handled there to keep the end-once contract simple).
        """

        content = self._delta_text(delta)
        if not content:
            return
        self._buffer += content
        yield from self._emit_progress()

    def _emit_progress(self) -> Iterator[StreamChunk]:
        """Re-extract spans and emit start / args-delta chunks for growth.

        Uses :meth:`_enumerate_payloads`, which includes a *trailing
        unterminated* ``<tool_call>`` whose body already decodes (raw_decode
        tolerates the missing ``</tool_call>``). That is what lets a ``say``
        call start streaming — and feeding ``speech`` suffixes into the
        StreamEmitter — the instant its JSON object is parseable, well before
        the close tag arrives.
        """

        for index, payload in self._enumerate_payloads().items():
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                continue
            state = self._states.setdefault(
                index,
                {
                    "id": None,
                    "name": None,
                    "arguments_emitted": "",
                    "started_yielded": False,
                    "ended": False,
                },
            )
            if state["name"] is None:
                state["name"] = name
            if not state["id"]:
                raw_id = payload.get("id")
                state["id"] = (
                    raw_id if isinstance(raw_id, str) and raw_id else f"call_{uuid4().hex[:8]}"
                )
            if not state["started_yielded"]:
                state["started_yielded"] = True
                yield StreamChunk(
                    kind="tool_call_start",
                    tool_call_id=cast(str, state["id"]),
                    name=cast(str, state["name"]),
                )
            # Surface the growing ``arguments`` JSON as suffix deltas so the
            # StreamEmitter's PartialJsonParser can flush ``speech_delta`` mid
            # stream. We serialise the parsed arguments deterministically each
            # tick and emit only the new tail.
            arguments = payload.get("arguments")
            if isinstance(arguments, dict):
                serialised = json.dumps(arguments, ensure_ascii=False)
                already = cast(str, state["arguments_emitted"])
                if serialised.startswith(already) and len(serialised) > len(already):
                    suffix = serialised[len(already) :]
                    state["arguments_emitted"] = serialised
                    yield StreamChunk(
                        kind="tool_call_args_delta",
                        tool_call_id=cast(str, state["id"]),
                        args_delta=suffix,
                    )

    def finish(self) -> Iterator[StreamChunk]:
        """Flush ``tool_call_end`` for every resolved span.

        Runs one final extraction over the whole buffer (closed spans) and, as
        a fallback, recovers a trailing ``<tool_call>`` whose ``</tool_call>``
        never arrived but whose body still decodes — so a truncated stream
        still resolves its last call. Each resolved-and-started span emits
        exactly one ``tool_call_end`` carrying the fully-parsed arguments.
        """

        # Re-run progress first so any span that completed on the last feed
        # (start / args-delta) is reflected before we close it out.
        yield from self._emit_progress()

        for index, payload in self._enumerate_payloads().items():
            state = self._states.get(index)
            if state is None or not state["started_yielded"] or state["ended"]:
                continue
            state["ended"] = True
            arguments = payload.get("arguments")
            final_arguments = arguments if isinstance(arguments, dict) else {}
            yield StreamChunk(
                kind="tool_call_end",
                tool_call_id=cast(str, state["id"]),
                final_arguments=cast(dict[str, Any], final_arguments),
            )

    def _enumerate_payloads(self) -> dict[int, dict[str, Any]]:
        """Decoded named payloads per span index, incl. a salvaged trailing one.

        Closed ``<tool_call>…</tool_call>`` spans come from the normal
        extraction. If the buffer also ends with an *unterminated*
        ``<tool_call>`` whose body already decodes (raw_decode tolerates the
        missing close tag), it is added at the next index — so the same
        positional index is used by :meth:`_emit_progress` (to start streaming
        it early) and :meth:`finish` (to close it out), and a truncated final
        call is recovered rather than dropped. Only payloads that decode to a
        *named* call are kept (a half-written ``{"nam`` decodes to nothing yet
        and is simply absent until the next tick grows it).
        """

        payloads: dict[int, dict[str, Any]] = {}
        spans = _extract_spans(self._buffer)
        for index, span in enumerate(spans):
            payload = _decode_tool_call_body(span)
            if payload is not None and _to_tool_call(payload) is not None:
                payloads[index] = payload

        # Salvage a trailing unterminated <tool_call> (truncated / mid-stream).
        match = _UNTERMINATED_TOOL_CALL_RE.search(self._buffer)
        if match is not None:
            payload = _decode_tool_call_body(match.group(1))
            if payload is not None and _to_tool_call(payload) is not None:
                payloads[len(spans)] = payload
        return payloads

    @staticmethod
    def _delta_text(delta: Any) -> str:
        """Pull plain-text content off a provider delta (``""`` when absent)."""

        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        return ""

    @property
    def log_calls(self) -> list[dict[str, Any]]:
        """Resolved calls in the ``{"id","name","arguments"}`` log shape.

        Mirrors the native parser's property so the core's ``finally`` logging
        block reconstructs the same ``raw_for_log`` / debug ``tool_calls``
        payload regardless of codec. ``arguments`` is kept as the serialised
        *string* (matching the native parser, which logs the accumulated arg
        string) so the debug surface is shape-identical across backends.
        """

        calls: list[dict[str, Any]] = []
        for index, payload in self._enumerate_payloads().items():
            state = self._states.get(index)
            if state is None or not state["started_yielded"]:
                continue
            arguments = payload.get("arguments")
            serialised = json.dumps(arguments if isinstance(arguments, dict) else {})
            calls.append(
                {
                    "id": state["id"],
                    "name": state["name"],
                    "arguments": serialised,
                }
            )
        return calls


__all__ = ["HermesToolCodec"]
