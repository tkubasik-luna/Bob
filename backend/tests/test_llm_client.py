"""Tests for :mod:`bob.llm_client`."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bob.config import Settings
from bob.llm import LLMResponse, ToolDefinition
from bob.llm_client import (
    ClaudeCliClient,
    LLMClientError,
    LMStudioClient,
    _repair_json_braces,
)

from .fixtures.tool_calling import (
    CLAUDE_FENCED,
    CLAUDE_MALFORMED_REPAIR,
    CLAUDE_WELL_FORMED,
    NATIVE_MALFORMED_ARGUMENTS_RAW,
    NATIVE_WELL_FORMED,
    ClaudeToolCallFixture,
    MalformedRepairFixture,
    NativeToolCallFixture,
)


def _make_settings() -> Settings:
    return Settings(
        LLM_BASE_URL="http://localhost:1234/v1",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS=12.5,
    )


def _patch_openai(client: LMStudioClient, response_text: str = "hello back") -> AsyncMock:
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]
    return create


@pytest.mark.asyncio
async def test_chat_calls_openai_with_expected_params() -> None:
    client = LMStudioClient(_make_settings())
    create = _patch_openai(client)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    result = await client.chat(messages=messages)

    assert result == "hello back"
    create.assert_awaited_once()
    assert create.await_args is not None
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["messages"] == messages
    assert kwargs["timeout"] == 12.5
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_chat_with_schema_passes_response_format() -> None:
    client = LMStudioClient(_make_settings())
    create = _patch_openai(client, response_text='{"ok": true}')

    schema = {"name": "Reply", "schema": {"type": "object"}}
    messages: list[dict[str, Any]] = [{"role": "user", "content": "structured?"}]

    result = await client.chat(messages=messages, schema=schema)

    assert result == '{"ok": true}'
    assert create.await_args is not None
    kwargs = create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_schema", "json_schema": schema}


@pytest.mark.asyncio
async def test_chat_logs_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LMStudioClient(_make_settings())
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22),
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("bob.llm_client.log_llm_call", _spy)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    await client.chat(messages=messages, session_id="sess-42")

    assert captured["session_id"] == "sess-42"
    assert captured["messages"] == messages
    assert captured["raw_response"] == "ok"
    assert captured["tokens_in"] == 11
    assert captured["tokens_out"] == 22
    assert isinstance(captured["latency_ms"], float)


@pytest.mark.asyncio
async def test_chat_raises_when_content_is_empty() -> None:
    """Empty LLM content surfaces as ``LLMClientError`` rather than ``""``.

    Pre-fix the empty string slipped through and the sub-agent runner
    reported it as ``"sub-agent response invalid: invalid JSON: Expecting
    value (line 1, column 1)"`` — misleading. The provider-side failure
    (model unloaded, context overflow, abrupt abort) now surfaces with
    a clear message and a ``finish_reason`` hint.
    """

    client = LMStudioClient(_make_settings())
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None),
                    finish_reason="length",
                )
            ]
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    with pytest.raises(LLMClientError, match="empty content"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# ClaudeCliClient
# ---------------------------------------------------------------------------


def _claude_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "LLM_PROVIDER": "claude_cli",
        "CLAUDE_CLI_BIN": "claude",
        "CLAUDE_CLI_TIMEOUT_SECONDS": 30.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.received_stdin: bytes | None = None

    async def communicate(self, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
        self.received_stdin = input_bytes
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_claude_cli_chat_invokes_subprocess_with_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings(CLAUDE_CLI_MODEL="sonnet"))
    fake_proc = _FakeProc(
        stdout=json.dumps(
            {
                "result": '{"speech": "hi"}',
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        ).encode("utf-8")
    )
    captured_argv: dict[str, Any] = {}

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        captured_argv["argv"] = list(argv)
        captured_argv["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    schema = {"name": "Reply", "schema": {"type": "object"}}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are bob"},
        {"role": "user", "content": "hello"},
    ]

    result = await client.chat(messages=messages, schema=schema, session_id="s1")

    assert result == '{"speech": "hi"}'
    argv = captured_argv["argv"]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--system-prompt" in argv
    sys_prompt = argv[argv.index("--system-prompt") + 1]
    assert sys_prompt.startswith("you are bob")
    assert "JSON Schema" in sys_prompt
    assert '"type": "object"' in sys_prompt
    assert "--json-schema" not in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert fake_proc.received_stdin == b"hello"
    # Isolation defaults on: quarantine from the user's ~/.claude.
    assert "--strict-mcp-config" in argv
    assert "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == ""
    assert captured_argv["kwargs"].get("cwd") is not None


@pytest.mark.asyncio
async def test_claude_cli_chat_isolation_disabled_inherits_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings(CLAUDE_CLI_ISOLATED=False))
    fake_proc = _FakeProc(stdout=json.dumps({"result": "hi"}).encode("utf-8"))
    captured_argv: dict[str, Any] = {}

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        captured_argv["argv"] = list(argv)
        captured_argv["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    await client.chat(messages=[{"role": "user", "content": "hello"}])

    argv = captured_argv["argv"]
    assert "--strict-mcp-config" not in argv
    assert "--setting-sources" not in argv
    assert captured_argv["kwargs"].get("cwd") is None


@pytest.mark.asyncio
async def test_claude_cli_chat_raises_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())
    fake_proc = _FakeProc(stdout=b"", stderr=b"boom", returncode=2)

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    with pytest.raises(LLMClientError, match="exited with code 2"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_claude_cli_chat_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings(CLAUDE_CLI_BIN="nope-not-here"))

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        raise FileNotFoundError("nope-not-here")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    with pytest.raises(LLMClientError, match="not found"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_claude_cli_chat_falls_back_when_stdout_not_wrapper_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())
    fake_proc = _FakeProc(stdout=b"plain text reply")

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result == "plain text reply"


@pytest.mark.asyncio
async def test_claude_cli_chat_raises_when_wrapper_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())
    fake_proc = _FakeProc(
        stdout=json.dumps({"result": "Not logged in", "is_error": True}).encode("utf-8")
    )

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    with pytest.raises(LLMClientError, match="Not logged in"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_claude_cli_chat_strips_markdown_code_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())
    fenced = '```json\n{"speech": "ok", "ui": []}\n```'
    fake_proc = _FakeProc(stdout=json.dumps({"result": fenced, "is_error": False}).encode("utf-8"))

    async def _fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result == '{"speech": "ok", "ui": []}'


def test_claude_cli_render_history_concatenates_multi_turn() -> None:
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    rendered = ClaudeCliClient._render_history(history)
    assert "first" in rendered and "reply" in rendered and "second" in rendered
    assert rendered.rstrip().endswith("second")


# ---------------------------------------------------------------------------
# Role-leak regression (issue 0048 post-mortem — logs 2026-05-28 11:46/11:48).
# A stale backend ran pre-fold code and shipped a ``system_validator`` row to
# LM Studio, which returned an opaque HTTP 400. The fold is now in place on
# all three OpenAI-bound paths and ``_assert_standard_roles`` raises loudly
# if any future regression drops a fold call — so we never ship a non-standard
# role over the wire again.
# ---------------------------------------------------------------------------


def test_assert_standard_roles_passes_for_valid_messages() -> None:
    from bob.llm_client import _assert_standard_roles

    _assert_standard_roles(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
            {"role": "tool", "content": "t"},
        ]
    )


def test_assert_standard_roles_raises_on_system_validator() -> None:
    from bob.llm_client import _assert_standard_roles

    with pytest.raises(LLMClientError, match="system_validator"):
        _assert_standard_roles(
            [
                {"role": "user", "content": "u"},
                {"role": "system_validator", "content": "feedback"},
            ]
        )


@pytest.mark.asyncio
async def test_chat_folds_system_validator_before_dispatch() -> None:
    """``chat`` must NOT leak ``system_validator`` to the OpenAI endpoint.

    Without the fold the call would surface the LM Studio 400 we hit in
    production on 2026-05-28; with the fold the post-dispatch messages
    contain only standard roles and the validator content is prefixed
    into a ``system`` message.
    """

    client = LMStudioClient(_make_settings())
    create = _patch_openai(client)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {"role": "system_validator", "content": "your last output was invalid"},
    ]
    await client.chat(messages=messages)

    create.assert_awaited_once()
    assert create.await_args is not None
    sent = create.await_args.kwargs["messages"]
    sent_roles = [m["role"] for m in sent]
    assert "system_validator" not in sent_roles
    assert sent_roles == ["user", "system"]
    # Validator content survives, just under a standard role.
    assert "invalid" in sent[1]["content"]


# ---------------------------------------------------------------------------
# Golden tool-calling fixtures (PRD 0008 / issue 0057).
#
# These lock the CURRENT behaviour of two of the three divergent tool-calling
# parse paths so the later 0008 refactor phases are regression-checked at PR
# time. They assert NO new behaviour — only what the code does today. Fixture
# data lives in ``tests/fixtures/tool_calling.py`` so later phases re-import the
# same cases. The third path (sub-agent envelope) is locked in
# ``tests/test_sub_agent_v2_runner.py``.
# ---------------------------------------------------------------------------


def _golden_tool() -> ToolDefinition:
    """A tool definition broad enough to carry every golden fixture's args."""

    return ToolDefinition(
        name="spawn_subtask",
        description="Spawn a background subtask.",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )


def _patch_openai_native_call(
    client: LMStudioClient, *, name: str, arguments_raw: str
) -> AsyncMock:
    """Stub one native ``message.tool_calls`` entry (LM Studio shape)."""

    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call_abc",
                                type="function",
                                function=SimpleNamespace(name=name, arguments=arguments_raw),
                            )
                        ],
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=9),
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]
    return create


# --- Path 1: Jarvis + LM Studio native (message.tool_calls) ----------------


@pytest.mark.parametrize("fx", NATIVE_WELL_FORMED, ids=lambda fx: fx.id)
@pytest.mark.asyncio
async def test_golden_native_well_formed_tool_call(fx: NativeToolCallFixture) -> None:
    """Native path: ``function.arguments`` JSON string → parsed ``ToolCall``.

    Empty-string arguments decode to ``{}`` (current ``arguments_raw or {}``
    branch). No brace-repair is applied on this path.
    """

    client = LMStudioClient(_make_settings())
    _patch_openai_native_call(client, name=fx.name, arguments_raw=fx.arguments_raw)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_golden_tool()],
    )

    assert isinstance(response, LLMResponse)
    assert response.is_tool_call is True
    assert response.text is None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == fx.expected_name
    assert call.arguments == fx.expected_arguments


@pytest.mark.asyncio
async def test_golden_native_malformed_arguments_raise() -> None:
    """Native path TODAY hard-fails on non-JSON ``function.arguments``.

    Unlike the Claude CLI path, the native path has no salvage pass — a
    non-JSON arguments string surfaces as ``LLMClientError``.
    """

    client = LMStudioClient(_make_settings())
    _patch_openai_native_call(
        client, name="spawn_subtask", arguments_raw=NATIVE_MALFORMED_ARGUMENTS_RAW
    )

    with pytest.raises(LLMClientError, match="not valid JSON"):
        await client.complete(
            messages=[{"role": "user", "content": "go"}],
            tools=[_golden_tool()],
        )


# --- Path 2: Jarvis + Claude CLI prompt-based ({"tool_calls":[…]}) ----------


def _patch_claude_chat(client: ClaudeCliClient, raw: str) -> None:
    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return raw

    client.chat = _fake_chat  # type: ignore[method-assign]


@pytest.mark.parametrize("fx", CLAUDE_WELL_FORMED, ids=lambda fx: fx.id)
@pytest.mark.asyncio
async def test_golden_claude_well_formed_tool_call(fx: ClaudeToolCallFixture) -> None:
    """Claude CLI path: ``{"tool_calls":[…]}`` (clean or trailing-prose) parses.

    ``raw_decode`` recognises the leading JSON object even when the model
    appends a confirmation sentence after the closing brace.
    """

    client = ClaudeCliClient(_claude_settings())
    _patch_claude_chat(client, fx.raw)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_golden_tool()],
    )

    assert response.is_tool_call is True
    assert response.text is None
    parsed = tuple((c.name, c.arguments) for c in response.tool_calls)
    assert parsed == fx.expected_calls


@pytest.mark.asyncio
async def test_golden_claude_fenced_tool_call_strips_fence() -> None:
    """Claude CLI path strips a ```` ```json ```` fence before parsing."""

    client = ClaudeCliClient(_claude_settings())
    _patch_claude_chat(client, CLAUDE_FENCED.raw)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_golden_tool()],
    )

    assert response.is_tool_call is True
    parsed = tuple((c.name, c.arguments) for c in response.tool_calls)
    assert parsed == CLAUDE_FENCED.expected_calls


#: The repair fixtures that, once salvaged, yield a ``tool_calls`` payload with
#: a recoverable first-call ``arguments`` dict (drives the end-to-end CLI test).
_CLAUDE_SALVAGEABLE_CALLS = tuple(
    fx for fx in CLAUDE_MALFORMED_REPAIR if fx.repairs_to_valid_json and fx.expected_arguments
)


@pytest.mark.parametrize("fx", _CLAUDE_SALVAGEABLE_CALLS, ids=lambda fx: fx.id)
@pytest.mark.asyncio
async def test_golden_claude_broken_braces_salvaged(fx: MalformedRepairFixture) -> None:
    """Claude CLI path salvages broken-brace tool calls via ``_repair_json_braces``.

    These ``raw_decode``-rejected strings are rebuilt from the open-stack and
    the tool call survives — without the repair the call would be dropped and
    the orchestrator would degrade to a "reformulate" fallback.
    """

    assert fx.repairs_to_valid_json is True
    assert fx.expected_arguments is not None
    client = ClaudeCliClient(_claude_settings())
    _patch_claude_chat(client, fx.raw)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_golden_tool()],
    )

    assert response.is_tool_call is True
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == fx.expected_arguments


@pytest.mark.parametrize("fx", CLAUDE_MALFORMED_REPAIR, ids=lambda fx: fx.id)
def test_golden_repair_json_braces_behaviour(fx: MalformedRepairFixture) -> None:
    """Lock ``_repair_json_braces`` output per fixture (the salvage primitive).

    ``repairs_to_valid_json`` records whether the repair returns a
    ``json.loads``-able string (vs ``None`` for the no-opener case). This is the
    unit the Claude CLI ``complete`` salvage branch is built on.
    """

    repaired = _repair_json_braces(fx.raw)
    if not fx.repairs_to_valid_json:
        assert repaired is None
        return
    assert repaired is not None
    parsed = json.loads(repaired)
    if fx.expected_arguments is not None:
        assert parsed["tool_calls"][0]["arguments"] == fx.expected_arguments
