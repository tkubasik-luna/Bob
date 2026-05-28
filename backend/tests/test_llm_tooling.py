"""Unit tests for the tool-calling codec layer (PRD 0008 / issue 0058).

Covers the three new seams in isolation:

- :class:`bob.llm.tooling.ToolSpec` derivation (from a Pydantic ``args_model``
  and from a legacy :class:`bob.llm.types.ToolDefinition`).
- :func:`bob.llm.tooling.select_codec` selection logic + the per-backend
  capability defaults + the ``LLM_TOOL_MODE`` override.
- :class:`bob.llm.tooling.NativeToolCodec` ``inject`` / ``parse`` /
  ``stream_parser``.

The native parse cases reuse the 0057 golden fixtures so this module and the
end-to-end ``test_llm_client.py`` assertions stay anchored to the same data.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from bob.llm.tooling import (
    BackendCapability,
    CodecNotAvailableError,
    NativeToolCallParseError,
    NativeToolCodec,
    ToolCodec,
    ToolSpec,
    capability_for_backend,
    select_codec,
)
from bob.llm.types import StreamChunk, ToolDefinition

from .fixtures.tool_calling import (
    NATIVE_MALFORMED_ARGUMENTS_RAW,
    NATIVE_WELL_FORMED,
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
    # parameters is the model's JSON Schema verbatim (no flattening in 0058).
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
    assert capability_for_backend("lm_studio") == BackendCapability(native_function_calling=True)
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


@pytest.mark.parametrize("mode", ["guided", "hermes"])
def test_select_codec_unimplemented_modes_raise(mode: str) -> None:
    # Declared-but-unimplemented: the capability supports it, but the codec
    # lands in a later issue, so selection raises a clear not-implemented error.
    capability = BackendCapability(guided_json=True, hermes_tags=True)
    with pytest.raises(CodecNotAvailableError, match="not implemented yet"):
        select_codec(capability, mode)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["guided", "hermes"])
def test_select_codec_explicit_mode_raises_when_capability_missing(mode: str) -> None:
    # Explicit mode against a capability that does not declare it → loud raise
    # naming the missing capability (not the not-implemented message).
    with pytest.raises(CodecNotAvailableError, match=f"does not declare {mode}"):
        select_codec(BackendCapability(native_function_calling=True), mode)  # type: ignore[arg-type]


def test_select_codec_auto_guided_only_backend_raises_not_implemented() -> None:
    with pytest.raises(CodecNotAvailableError, match="0060"):
        select_codec(BackendCapability(guided_json=True), "auto")


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
