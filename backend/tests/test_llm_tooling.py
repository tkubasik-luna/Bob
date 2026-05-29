"""Unit tests for the tool-calling codec layer (PRD 0008 / issues 0058 + 0061).

Covers the codec seams in isolation:

- :class:`bob.llm.tooling.ToolSpec` derivation (from a Pydantic ``args_model``
  and from a legacy :class:`bob.llm.types.ToolDefinition`).
- :func:`bob.llm.tooling.select_codec` selection logic + the per-backend
  capability defaults + the ``LLM_TOOL_MODE`` override.
- :class:`bob.llm.tooling.NativeToolCodec` ``inject`` / ``parse`` /
  ``stream_parser``.
- :class:`bob.llm.tooling.HermesToolCodec` (issue 0061) ``inject`` / ``parse``
  tolerant chain / streaming progressive view, plus a backend-swap parity check
  asserting Hermes (claude_cli) and Native (lm_studio) decode to the same
  :class:`bob.llm.types.ToolCall` for a clean call, a recovered single-quoted
  py-dict call, and plain text.

The native + Hermes parse cases reuse the 0057 golden fixtures so this module
and the end-to-end ``test_llm_client.py`` assertions stay anchored to the same
data.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from bob.llm.tooling import (
    BackendCapability,
    CodecNotAvailableError,
    HermesToolCodec,
    NativeToolCallParseError,
    NativeToolCodec,
    ToolCodec,
    ToolSpec,
    capability_for_backend,
    flatten_schema,
    order_specs,
    select_codec,
)
from bob.llm.types import StreamChunk, ToolDefinition

from .fixtures.tool_calling import (
    CLAUDE_FENCED,
    CLAUDE_TOLERANCE,
    CLAUDE_WELL_FORMED,
    NATIVE_MALFORMED_ARGUMENTS_RAW,
    NATIVE_WELL_FORMED,
    HermesToleranceFixture,
    HermesToolCallFixture,
    NativeToolCallFixture,
)

# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


class _SampleArgs(BaseModel):
    """A small model to exercise ``model_json_schema`` derivation."""

    title: str = Field(..., description="A title.")
    count: int = 1


def test_tool_spec_from_args_model_derives_parameters() -> None:
    spec = ToolSpec.from_args_model(
        name="sample",
        description="A sample tool.",
        args_model=_SampleArgs,
    )

    assert spec.name == "sample"
    assert spec.description == "A sample tool."
    assert spec.args_model is _SampleArgs
    # 0063 flattens at derivation, but _SampleArgs is already flat (no Optional
    # / $ref / nesting) so flattening is the identity here.
    assert spec.parameters == _SampleArgs.model_json_schema()
    assert spec.parameters["type"] == "object"
    assert set(spec.parameters["properties"]) == {"title", "count"}
    assert spec.parameters["required"] == ["title"]


def test_tool_spec_from_tool_definition_wraps_dict_schema() -> None:
    definition = ToolDefinition(
        name="spawn_subtask",
        description="Spawn a background subtask.",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )

    spec = ToolSpec.from_tool_definition(definition)

    assert spec.name == "spawn_subtask"
    assert spec.description == "Spawn a background subtask."
    assert spec.parameters == definition.parameters
    # The wire-level ToolDefinition does not retain the Pydantic model.
    assert spec.args_model is None


def test_tool_spec_round_trips_to_tool_definition() -> None:
    definition = ToolDefinition(
        name="say",
        description="Speak.",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    assert ToolSpec.from_tool_definition(definition).to_tool_definition() == definition


# ---------------------------------------------------------------------------
# Capability defaults + select_codec
# ---------------------------------------------------------------------------


def test_capability_defaults_per_backend() -> None:
    # Issue 0060 — LM Studio declares ``guided_json`` too (its
    # ``response_format: json_schema`` constrained decode), used to gate the
    # sub-agent envelope. Native function calling stays declared so the Jarvis
    # tool-calling path is unchanged.
    assert capability_for_backend("lm_studio") == BackendCapability(
        native_function_calling=True, guided_json=True
    )
    assert capability_for_backend("claude_cli") == BackendCapability(hermes_tags=True)
    # Unknown backend → conservative all-off default.
    assert capability_for_backend("mystery") == BackendCapability()


def test_select_codec_auto_prefers_native() -> None:
    codec = select_codec(BackendCapability(native_function_calling=True), "auto")
    assert isinstance(codec, NativeToolCodec)
    # NativeToolCodec satisfies the ToolCodec protocol.
    assert isinstance(codec, ToolCodec)


def test_select_codec_native_mode_returns_native() -> None:
    codec = select_codec(BackendCapability(native_function_calling=True), "native")
    assert isinstance(codec, NativeToolCodec)


def test_select_codec_native_mode_raises_when_unsupported() -> None:
    with pytest.raises(CodecNotAvailableError, match="native_function_calling"):
        select_codec(BackendCapability(native_function_calling=False), "native")


def test_select_codec_auto_raises_when_no_format_supported() -> None:
    with pytest.raises(CodecNotAvailableError, match="no supported"):
        select_codec(BackendCapability(), "auto")


def test_select_codec_guided_mode_raises_not_implemented() -> None:
    # Declared-but-unimplemented: the capability supports guided JSON, but its
    # codec lands in issue 0060, so selection raises a clear not-implemented error.
    capability = BackendCapability(guided_json=True, hermes_tags=True)
    with pytest.raises(CodecNotAvailableError, match="not implemented yet"):
        select_codec(capability, "guided")


def test_select_codec_hermes_mode_returns_hermes() -> None:
    # Issue 0061 implemented the Hermes codec: an explicit hermes mode against a
    # hermes-capable backend now returns it (no longer the not-implemented raise).
    codec = select_codec(BackendCapability(hermes_tags=True), "hermes")
    assert isinstance(codec, HermesToolCodec)
    assert isinstance(codec, ToolCodec)


@pytest.mark.parametrize("mode", ["guided", "hermes"])
def test_select_codec_explicit_mode_raises_when_capability_missing(mode: str) -> None:
    # Explicit mode against a capability that does not declare it → loud raise
    # naming the missing capability (not the not-implemented message).
    with pytest.raises(CodecNotAvailableError, match=f"does not declare {mode}"):
        select_codec(BackendCapability(native_function_calling=True), mode)  # type: ignore[arg-type]


def test_select_codec_auto_guided_only_backend_raises_not_implemented() -> None:
    with pytest.raises(CodecNotAvailableError, match="0060"):
        select_codec(BackendCapability(guided_json=True), "auto")


def test_select_codec_auto_hermes_only_backend_returns_hermes() -> None:
    # The claude_cli capability shape: hermes-only → auto resolves to Hermes.
    codec = select_codec(capability_for_backend("claude_cli"), "auto")
    assert isinstance(codec, HermesToolCodec)


# ---------------------------------------------------------------------------
# NativeToolCodec.inject
# ---------------------------------------------------------------------------


def test_native_inject_empty_specs_returns_empty() -> None:
    assert NativeToolCodec().inject([], []) == {}


def test_native_inject_builds_openai_tools_block_in_order() -> None:
    specs = [
        ToolSpec(name="a", description="A", parameters={"type": "object"}),
        ToolSpec(name="b", description="B", parameters={"type": "object"}),
    ]
    kwargs = NativeToolCodec().inject([], specs)

    assert kwargs["tool_choice"] == "auto"
    names = [entry["function"]["name"] for entry in kwargs["tools"]]
    # Injection order is stable (registration order in == out).
    assert names == ["a", "b"]
    assert kwargs["tools"][0] == {
        "type": "function",
        "function": {"name": "a", "description": "A", "parameters": {"type": "object"}},
    }


# ---------------------------------------------------------------------------
# NativeToolCodec.parse (non-streaming) — reuses 0057 fixtures
# ---------------------------------------------------------------------------


def _native_message(*, name: str, arguments_raw: str, call_id: str | None = "call_abc") -> Any:
    """Build a fake provider ``message`` carrying one native tool call."""

    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id=call_id,
                type="function",
                function=SimpleNamespace(name=name, arguments=arguments_raw),
            )
        ],
    )


@pytest.mark.parametrize("fx", NATIVE_WELL_FORMED, ids=lambda fx: fx.id)
def test_native_parse_well_formed(fx: NativeToolCallFixture) -> None:
    calls = NativeToolCodec().parse(_native_message(name=fx.name, arguments_raw=fx.arguments_raw))

    assert len(calls) == 1
    assert calls[0].name == fx.expected_name
    assert calls[0].arguments == fx.expected_arguments
    assert calls[0].id == "call_abc"


def test_native_parse_no_tool_calls_returns_empty() -> None:
    assert NativeToolCodec().parse(SimpleNamespace(content="hi", tool_calls=None)) == []


def test_native_parse_missing_id_gets_placeholder() -> None:
    calls = NativeToolCodec().parse(
        _native_message(name="say", arguments_raw='{"speech": "hi"}', call_id=None)
    )
    assert calls[0].id.startswith("call_")


def test_native_parse_malformed_raises_decode_error() -> None:
    with pytest.raises(NativeToolCallParseError, match="not valid JSON") as excinfo:
        NativeToolCodec().parse(
            _native_message(name="spawn_subtask", arguments_raw=NATIVE_MALFORMED_ARGUMENTS_RAW)
        )
    assert excinfo.value.is_decode_error is True
    assert excinfo.value.arguments_raw == NATIVE_MALFORMED_ARGUMENTS_RAW


def test_native_parse_non_object_arguments_raise_without_decode_flag() -> None:
    with pytest.raises(NativeToolCallParseError, match="must decode to an object") as excinfo:
        NativeToolCodec().parse(_native_message(name="x", arguments_raw="[1, 2, 3]"))
    # Decoded fine, just wrong shape → no debug event in the core.
    assert excinfo.value.is_decode_error is False


# ---------------------------------------------------------------------------
# NativeToolCodec.stream_parser
# ---------------------------------------------------------------------------


def _delta(
    *,
    index: int = 0,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> Any:
    """Build a fake ``choices[0].delta`` carrying one tool-call entry."""

    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ],
    )


def test_native_stream_lifecycle_start_delta_end() -> None:
    parser = NativeToolCodec().stream_parser()
    chunks: list[StreamChunk] = []

    chunks += list(parser.feed(_delta(call_id="call_1", name="say")))
    chunks += list(parser.feed(_delta(arguments='{"speech":')))
    chunks += list(parser.feed(_delta(arguments=' "hi"}')))
    chunks += list(parser.finish())

    kinds = [c.kind for c in chunks]
    assert kinds == [
        "tool_call_start",
        "tool_call_args_delta",
        "tool_call_args_delta",
        "tool_call_end",
    ]
    assert chunks[0].name == "say"
    assert chunks[0].tool_call_id == "call_1"
    # Argument suffixes flow through byte-identical (the speech_delta contract).
    assert chunks[1].args_delta == '{"speech":'
    assert chunks[2].args_delta == ' "hi"}'
    assert chunks[3].final_arguments == {"speech": "hi"}


def test_native_stream_args_before_name_are_held_until_start() -> None:
    """A delta carrying args before the name resolves must not emit an args
    chunk before ``tool_call_start`` (the StreamEmitter binds msg_id on the
    first frame)."""

    parser = NativeToolCodec().stream_parser()

    # First tick: arguments arrive but no name yet → nothing emitted.
    early = list(parser.feed(_delta(call_id="call_9", arguments='{"a":1}')))
    assert early == []

    # Second tick: name resolves → start fires; the already-buffered args were
    # accumulated and surface in the final parse.
    later = list(parser.feed(_delta(name="spawn_subtask")))
    assert [c.kind for c in later] == ["tool_call_start"]

    end = list(parser.finish())
    assert end[0].final_arguments == {"a": 1}


def test_native_stream_missing_id_gets_placeholder() -> None:
    parser = NativeToolCodec().stream_parser()
    start = list(parser.feed(_delta(name="say", arguments="{}")))
    assert start[0].kind == "tool_call_start"
    assert start[0].tool_call_id is not None
    assert start[0].tool_call_id.startswith("call_")


def test_native_stream_unresolved_name_skipped_in_finish() -> None:
    parser = NativeToolCodec().stream_parser()
    # Args but never a name → no start, and finish() emits nothing.
    list(parser.feed(_delta(arguments='{"x":1}')))
    assert list(parser.finish()) == []
    assert parser.log_calls == []


def test_native_stream_malformed_final_args_raise() -> None:
    parser = NativeToolCodec().stream_parser()
    list(parser.feed(_delta(call_id="c1", name="say", arguments="{not json")))
    with pytest.raises(NativeToolCallParseError, match="after stream close") as excinfo:
        list(parser.finish())
    assert excinfo.value.is_decode_error is True


def test_native_stream_log_calls_shape() -> None:
    parser = NativeToolCodec().stream_parser()
    list(parser.feed(_delta(call_id="c1", name="say", arguments='{"speech": "hi"}')))
    # log_calls keeps arguments as the accumulated STRING (not parsed) —
    # matches the core's raw-for-log reconstruction.
    assert parser.log_calls == [{"id": "c1", "name": "say", "arguments": '{"speech": "hi"}'}]


# ---------------------------------------------------------------------------
# HermesToolCodec.inject (issue 0061)
# ---------------------------------------------------------------------------


def _hermes_spec(name: str = "spawn_subtask") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"Tool {name}.",
        parameters={"type": "object", "properties": {"title": {"type": "string"}}},
    )


def test_hermes_inject_empty_specs_returns_empty_no_block() -> None:
    messages = [{"role": "system", "content": "you are bob"}]
    assert HermesToolCodec().inject(messages, []) == {}
    # No specs → message left untouched (pure passthrough, matches the old guard).
    assert messages == [{"role": "system", "content": "you are bob"}]


def test_hermes_inject_appends_block_to_existing_system_message() -> None:
    messages = [
        {"role": "system", "content": "you are bob"},
        {"role": "user", "content": "hi"},
    ]
    kwargs = HermesToolCodec().inject(messages, [_hermes_spec()])

    # Prompt is the whole contract → no per-call request kwargs.
    assert kwargs == {}
    system = messages[0]["content"]
    assert system.startswith("you are bob")
    assert "<tools>" in system and "</tools>" in system
    # The emission protocol + the OpenAI-style function entry are present.
    assert "<tool_call>" in system
    assert '"name": "spawn_subtask"' in system
    # The user message is untouched.
    assert messages[1] == {"role": "user", "content": "hi"}


def test_hermes_inject_prepends_system_message_when_none() -> None:
    messages = [{"role": "user", "content": "hi"}]
    HermesToolCodec().inject(messages, [_hermes_spec()])

    assert messages[0]["role"] == "system"
    # Prepended block is lstripped (no leading blank lines on a fresh system msg).
    assert messages[0]["content"].startswith("You are a function-calling AI model")
    assert messages[1] == {"role": "user", "content": "hi"}


def test_hermes_inject_preserves_tool_order() -> None:
    messages: list[dict[str, Any]] = []
    HermesToolCodec().inject(messages, [_hermes_spec("a"), _hermes_spec("b")])
    block = messages[0]["content"]
    # Registration order in == lines out (ordering hygiene is issue 0063).
    assert block.index('"name": "a"') < block.index('"name": "b"')


# ---------------------------------------------------------------------------
# HermesToolCodec.parse — reuses the 0057 path-2 fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fx", CLAUDE_WELL_FORMED, ids=lambda fx: fx.id)
def test_hermes_parse_well_formed(fx: HermesToolCallFixture) -> None:
    calls = HermesToolCodec().parse(fx.raw)
    assert tuple((c.name, c.arguments) for c in calls) == fx.expected_calls


def test_hermes_parse_fenced_body_strips_fence() -> None:
    calls = HermesToolCodec().parse(CLAUDE_FENCED.raw)
    assert tuple((c.name, c.arguments) for c in calls) == CLAUDE_FENCED.expected_calls


@pytest.mark.parametrize("fx", CLAUDE_TOLERANCE, ids=lambda fx: fx.id)
def test_hermes_parse_tolerant_chain(fx: HermesToleranceFixture) -> None:
    # expected_calls == () means the reply degrades to plain text (no calls).
    calls = HermesToolCodec().parse(fx.raw)
    assert tuple((c.name, c.arguments) for c in calls) == fx.expected_calls


def test_hermes_parse_generates_call_id_when_missing() -> None:
    # The clean fixture body carries no id → placeholder minted (native scheme).
    [call] = HermesToolCodec().parse(
        '<tool_call>{"name": "say", "arguments": {"speech": "hi"}}</tool_call>'
    )
    assert call.id.startswith("call_")


def test_hermes_parse_keeps_supplied_call_id() -> None:
    [call] = HermesToolCodec().parse(CLAUDE_WELL_FORMED[0].raw)
    # hermes/clean carries call_99 in the body — preserved, not regenerated.
    assert call.id == "call_99"


def test_hermes_parse_nameless_span_skipped() -> None:
    # A decodable object with no name is not a dispatchable call → skipped.
    assert HermesToolCodec().parse('<tool_call>{"arguments": {"x": 1}}</tool_call>') == []


def test_hermes_parse_non_object_arguments_coerced_to_empty() -> None:
    # The tolerant chain favours recovering *something*: a non-object arguments
    # is coerced to {} rather than raising (strict re-validation is issue 0062).
    [call] = HermesToolCodec().parse('<tool_call>{"name": "say", "arguments": [1, 2]}</tool_call>')
    assert (call.name, call.arguments) == ("say", {})


# ---------------------------------------------------------------------------
# HermesToolCodec.stream_parser — progressive view + truncation salvage
# ---------------------------------------------------------------------------


def _text_delta(content: str) -> Any:
    """A provider delta carrying plain-text content (Hermes streams as text)."""

    return SimpleNamespace(content=content)


def test_hermes_stream_progressive_lifecycle() -> None:
    parser = HermesToolCodec().stream_parser()
    chunks: list[StreamChunk] = []
    # A single closed call delivered across three text deltas.
    chunks += list(parser.feed(_text_delta('<tool_call>{"name": "say", ')))
    chunks += list(parser.feed(_text_delta('"arguments": {"speech": "hello ')))
    chunks += list(parser.feed(_text_delta('world"}}</tool_call>')))
    chunks += list(parser.finish())

    kinds = [c.kind for c in chunks]
    assert kinds[0] == "tool_call_start"
    assert kinds[-1] == "tool_call_end"
    assert "tool_call_args_delta" in kinds
    assert chunks[0].name == "say"
    # The args deltas reassemble to the full serialised arguments (the
    # speech_delta contract: PartialJsonParser sees a growing JSON object).
    reassembled = "".join(c.args_delta or "" for c in chunks if c.kind == "tool_call_args_delta")
    assert reassembled == '{"speech": "hello world"}'
    end = next(c for c in chunks if c.kind == "tool_call_end")
    assert end.final_arguments == {"speech": "hello world"}


def test_hermes_stream_salvages_unterminated_trailing_call() -> None:
    parser = HermesToolCodec().stream_parser()
    chunks: list[StreamChunk] = []
    # Stream truncates: the body decodes (raw_decode tolerates the missing
    # close tag) but </tool_call> never arrives.
    chunks += list(parser.feed(_text_delta('<tool_call>{"name": "say", ')))
    chunks += list(parser.feed(_text_delta('"arguments": {"speech": "hi"}}')))
    chunks += list(parser.finish())

    kinds = [c.kind for c in chunks]
    assert "tool_call_start" in kinds
    assert kinds[-1] == "tool_call_end"
    end = next(c for c in chunks if c.kind == "tool_call_end")
    assert end.final_arguments == {"speech": "hi"}


def test_hermes_stream_no_tags_emits_nothing() -> None:
    parser = HermesToolCodec().stream_parser()
    assert list(parser.feed(_text_delta("just prose, no call"))) == []
    assert list(parser.finish()) == []
    assert parser.log_calls == []


def test_hermes_stream_log_calls_shape() -> None:
    parser = HermesToolCodec().stream_parser()
    list(parser.feed(_text_delta('<tool_call>{"name": "say", "arguments": {"speech": "hi"}}')))
    [logged] = parser.log_calls
    # Mirrors the native parser: arguments serialised to a STRING for logging.
    assert logged["name"] == "say"
    assert logged["arguments"] == '{"speech": "hi"}'
    assert str(logged["id"]).startswith("call_")


# ---------------------------------------------------------------------------
# Backend-swap parity — Hermes (claude_cli) vs Native (lm_studio)
# ---------------------------------------------------------------------------


def _native_one(name: str, arguments_raw: str) -> Any:
    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_x",
                type="function",
                function=SimpleNamespace(name=name, arguments=arguments_raw),
            )
        ],
    )


def test_backend_swap_parity_well_formed_call() -> None:
    """A well-formed call decodes to the same (name, arguments) on both seams."""

    hermes = select_codec(capability_for_backend("claude_cli"), "auto")
    native = select_codec(capability_for_backend("lm_studio"), "auto")

    hermes_calls = hermes.parse(
        '<tool_call>{"name": "say", "arguments": {"speech": "hi"}}</tool_call>'
    )
    native_calls = native.parse(_native_one("say", '{"speech": "hi"}'))

    assert [(c.name, c.arguments) for c in hermes_calls] == [("say", {"speech": "hi"})]
    assert [(c.name, c.arguments) for c in hermes_calls] == [
        (c.name, c.arguments) for c in native_calls
    ]


def test_backend_swap_parity_recovered_py_dict_call() -> None:
    """A single-quoted py-dict body the Hermes chain recovers via ast.literal_eval
    matches what Native decodes from the equivalent JSON-string arguments."""

    hermes = select_codec(capability_for_backend("claude_cli"), "auto")
    native = select_codec(capability_for_backend("lm_studio"), "auto")

    hermes_calls = hermes.parse(
        "<tool_call>{'name': 'say', 'arguments': {'speech': 'hi'}}</tool_call>"
    )
    native_calls = native.parse(_native_one("say", '{"speech": "hi"}'))

    assert [(c.name, c.arguments) for c in hermes_calls] == [
        (c.name, c.arguments) for c in native_calls
    ]


def test_backend_swap_parity_plain_text_yields_no_calls() -> None:
    """Plain text → ``[]`` on both seams (Native sees no tool_calls field;
    Hermes finds no <tool_call> tag)."""

    hermes = select_codec(capability_for_backend("claude_cli"), "auto")
    native = select_codec(capability_for_backend("lm_studio"), "auto")

    assert hermes.parse("just a sentence, no tool call") == []
    assert native.parse(SimpleNamespace(content="just a sentence", tool_calls=None)) == []


# ---------------------------------------------------------------------------
# Schema hygiene — flatten_schema / order_specs (issue 0063)
# ---------------------------------------------------------------------------


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swap the schema module logger for a recorder, returning the event list."""

    events: list[str] = []
    monkeypatch.setattr(
        "bob.llm.tooling.schema._logger",
        SimpleNamespace(warning=lambda event, **_kwargs: events.append(event)),
    )
    return events


def test_flatten_schema_collapses_optional_anyof_to_single_branch() -> None:
    """``Optional[str]`` (``anyOf: [str, null]``) collapses to the lone non-null
    branch and carries the ``default`` / ``title`` / ``description`` siblings."""

    schema = {
        "type": "object",
        "properties": {
            "note": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Note",
                "description": "An optional note.",
            }
        },
    }

    note = flatten_schema(schema)["properties"]["note"]

    assert "anyOf" not in note
    assert note == {
        "type": "string",
        "default": None,
        "title": "Note",
        "description": "An optional note.",
    }


def test_flatten_schema_inlines_ref_and_drops_defs() -> None:
    """A ``$ref`` into ``$defs`` is inlined (sibling keys win) and the now-unused
    ``$defs`` container is dropped."""

    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Child", "description": "the child"}},
        "$defs": {
            "Child": {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
            }
        },
    }

    flat = flatten_schema(schema)

    assert "$defs" not in flat
    child = flat["properties"]["child"]
    assert "$ref" not in child
    assert child["type"] == "object"
    assert child["properties"]["x"] == {"type": "integer"}
    assert child["description"] == "the child"


def test_flatten_schema_collapses_string_const_union_to_enum() -> None:
    """A union of pure string ``const`` branches becomes a flat ``str`` + ``enum``,
    carrying the ``title`` sibling."""

    schema = {
        "type": "object",
        "properties": {
            "mode": {
                "anyOf": [
                    {"const": "read", "type": "string"},
                    {"const": "write", "type": "string"},
                ],
                "title": "Mode",
            }
        },
    }

    mode = flatten_schema(schema)["properties"]["mode"]

    assert "anyOf" not in mode
    assert mode == {"type": "string", "enum": ["read", "write"], "title": "Mode"}


def test_flatten_schema_warns_and_narrows_heterogeneous_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine ``str | int`` union cannot stay flat and complete: it is narrowed
    to its first branch (never silently dropped) and a warning is emitted."""

    events = _capture_warnings(monkeypatch)
    schema = {
        "type": "object",
        "properties": {"val": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
    }

    val = flatten_schema(schema)["properties"]["val"]

    assert val == {"type": "string"}  # first branch kept, not dropped to {}
    assert "tool_schema.flatten.union_narrowed" in events


def test_flatten_schema_caps_depth_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nesting past ``max_depth`` is replaced with a permissive placeholder and
    warned, rather than emitted deep enough to choke a grammar compiler."""

    events = _capture_warnings(monkeypatch)
    schema = {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {
                    "b": {
                        "type": "object",
                        "properties": {"c": {"type": "string"}},
                    }
                },
            }
        },
    }

    flat = flatten_schema(schema, max_depth=1)

    # ``b`` sits at depth 2 > max_depth 1 → placeholder, its ``c`` child dropped.
    assert flat["properties"]["a"]["properties"]["b"] == {"type": "object"}
    assert "tool_schema.flatten.depth_capped" in events


def test_flatten_schema_is_identity_on_flat_schema_and_never_mutates() -> None:
    """An already-flat schema round-trips unchanged, the input is not mutated,
    and flattening is idempotent."""

    flat_input = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "count": {"type": "integer", "default": 1},
        },
        "required": ["title"],
    }
    original = copy.deepcopy(flat_input)

    out = flatten_schema(flat_input)

    assert out == flat_input
    assert flat_input == original  # input untouched
    assert flatten_schema(out) == out  # idempotent


def test_order_specs_sorts_by_name() -> None:
    specs = [
        ToolSpec.from_args_model(name=name, description="", args_model=_SampleArgs)
        for name in ("zeta", "alpha", "mike")
    ]

    assert [spec.name for spec in order_specs(specs)] == ["alpha", "mike", "zeta"]


def test_order_specs_is_deterministic_regardless_of_input_order() -> None:
    alpha = ToolSpec.from_args_model(name="alpha", description="", args_model=_SampleArgs)
    mike = ToolSpec.from_args_model(name="mike", description="", args_model=_SampleArgs)
    zeta = ToolSpec.from_args_model(name="zeta", description="", args_model=_SampleArgs)

    one = [spec.name for spec in order_specs([zeta, alpha, mike])]
    two = [spec.name for spec in order_specs([mike, zeta, alpha])]

    assert one == two == ["alpha", "mike", "zeta"]
