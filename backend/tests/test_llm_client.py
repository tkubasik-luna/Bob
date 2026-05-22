"""Tests for :mod:`bob.llm_client`."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bob.config import Settings
from bob.llm_client import ClaudeCliClient, LLMClientError, LMStudioClient


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
async def test_chat_returns_empty_string_when_content_is_none() -> None:
    client = LMStudioClient(_make_settings())
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result == ""


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
