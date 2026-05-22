"""LLM client abstraction and LM Studio implementation.

The abstract :class:`LLMClient` is intentionally tiny — a single ``chat``
method returning the raw string emitted by the model. Higher layers
(``response_parser``, ``chat_service``) take care of validation, retries
and structured-output parsing.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any, cast
from uuid import uuid4

import structlog
from openai import AsyncOpenAI

from bob.config import Settings
from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.logging_setup import log_llm_call

_logger = structlog.get_logger(__name__)


class LLMClientError(RuntimeError):
    """Raised when an LLM backend fails irrecoverably (non-zero exit, timeout)."""


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence around a JSON payload.

    Some chat models — notably ``claude -p`` — like to wrap JSON in
    ```json ... ``` even when told not to. Returns ``text`` untouched if no
    fence is detected.
    """

    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    first = lines[0].lstrip("`").strip().lower()
    if first not in ("", "json"):
        return text
    body_end = len(lines)
    if lines[-1].strip().startswith("```"):
        body_end -= 1
    return "\n".join(lines[1:body_end]).strip()


class LLMClient(ABC):
    """Abstract interface for an OpenAI-compatible chat LLM."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Send ``messages`` to the LLM and return the raw response string.

        If ``schema`` is provided, ask the backend for a JSON response matching
        the given JSON Schema (LM Studio's structured output feature).
        ``session_id`` is purely passthrough for the call-log file — no business
        logic depends on it at this layer.
        """

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        """Send ``messages`` to the LLM and return either text or tool calls.

        When ``tools`` is supplied the backend is told the model may call any of
        them. The return value is an :class:`LLMResponse`:

        - ``text != None`` and ``tool_calls == []`` → the model answered with
          plain text. This is allowed even when ``tools`` was non-empty (the
          model just chose not to call anything).
        - ``text is None`` and ``tool_calls`` non-empty → the model wants to
          invoke one or more tools.

        Raises :class:`LLMClientError` if the backend returns a structurally
        invalid response (e.g. tool-call arguments that are not valid JSON).
        """


class LMStudioClient(LLMClient):
    """:class:`LLMClient` implementation wrapping ``openai.AsyncOpenAI``.

    Configured to talk to a local LM Studio instance (or any OpenAI-compatible
    endpoint) via :class:`Settings`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": schema,
            }

        started = time.perf_counter()
        completion = await self._client.chat.completions.create(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000.0

        message = completion.choices[0].message
        content = message.content or ""
        if not content:
            content = getattr(message, "reasoning_content", "") or ""
        raw = cast(str, content)

        tokens_in: int | None = None
        tokens_out: int | None = None
        usage = getattr(completion, "usage", None)
        if usage is not None:
            tokens_in = getattr(usage, "prompt_tokens", None)
            tokens_out = getattr(usage, "completion_tokens", None)

        log_llm_call(
            session_id=session_id,
            messages=messages,
            raw_response=raw,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        return raw

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            kwargs["tool_choice"] = "auto"

        started = time.perf_counter()
        completion = await self._client.chat.completions.create(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000.0

        message = completion.choices[0].message
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for raw_call in raw_tool_calls:
            function = getattr(raw_call, "function", None)
            if function is None:
                continue
            name = getattr(function, "name", None) or ""
            arguments_raw = getattr(function, "arguments", "") or ""
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError as exc:
                raise LLMClientError(
                    f"LM Studio tool call arguments are not valid JSON: {arguments_raw[:200]!r}"
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMClientError(
                    f"LM Studio tool call arguments must decode to an object, "
                    f"got {type(arguments).__name__}"
                )
            call_id = getattr(raw_call, "id", None) or f"call_{uuid4().hex[:8]}"
            tool_calls.append(
                ToolCall(id=call_id, name=name, arguments=cast(dict[str, Any], arguments))
            )

        text: str | None
        if tool_calls:
            text = None
            raw_for_log = json.dumps(
                [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls],
                ensure_ascii=False,
            )
        else:
            content = message.content or ""
            if not content:
                content = getattr(message, "reasoning_content", "") or ""
            text_value = cast(str, content)
            if not text_value:
                raise LLMClientError("LM Studio returned empty response")
            text = text_value
            raw_for_log = text_value

        tokens_in: int | None = None
        tokens_out: int | None = None
        usage = getattr(completion, "usage", None)
        if usage is not None:
            tokens_in = getattr(usage, "prompt_tokens", None)
            tokens_out = getattr(usage, "completion_tokens", None)

        log_llm_call(
            session_id=session_id,
            messages=messages,
            raw_response=raw_for_log,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        return LLMResponse(text=text, tool_calls=tool_calls)


class ClaudeCliClient(LLMClient):
    """:class:`LLMClient` implementation shelling out to the ``claude`` CLI.

    Each call spawns ``claude -p --output-format json`` and feeds the full
    conversation through ``--system-prompt`` + a serialized history on stdin.
    When a JSON schema is supplied it is appended to the system prompt as an
    instruction (the CLI's ``--json-schema`` flag only validates and silently
    drops invalid output, which would defeat the response-parser retry loop).
    Tools are disabled (``--tools ""``) since Bob only needs the chat reply.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def _split_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(str(msg.get("content", "")))
            else:
                rest.append(msg)
        return "\n\n".join(system_parts), rest

    @staticmethod
    def _render_history(history: list[dict[str, Any]]) -> str:
        """Serialize non-system history into a single prompt string.

        ``claude -p`` consumes one prompt per invocation. For multi-turn
        contexts we render the prior exchanges as labeled blocks and put the
        latest user message at the end so the model sees it as the live turn.
        """

        if not history:
            return ""
        if len(history) == 1 and history[0].get("role") == "user":
            return str(history[0].get("content", ""))

        lines: list[str] = ["Conversation so far:"]
        for msg in history[:-1]:
            role = str(msg.get("role", "user")).upper()
            content = str(msg.get("content", ""))
            lines.append(f"[{role}]\n{content}")
        last = history[-1]
        last_role = str(last.get("role", "user")).upper()
        lines.append(f"\nCurrent [{last_role}] message:\n{last.get('content', '')}")
        return "\n\n".join(lines)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        system_prompt, history = self._split_messages(messages)
        prompt = self._render_history(history)

        if schema is not None:
            schema_payload = schema.get("schema", schema)
            schema_instruction = (
                "\n\nIMPORTANT: réponds UNIQUEMENT par un objet JSON valide, "
                "sans aucun texte avant ou après, SANS bloc de code markdown "
                "(pas de ```json ni ```), conforme à ce JSON Schema :\n"
                f"{json.dumps(schema_payload, ensure_ascii=False)}"
            )
            system_prompt = (system_prompt + schema_instruction).strip()

        argv: list[str] = [
            self._settings.CLAUDE_CLI_BIN,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
        ]
        if system_prompt:
            argv += ["--system-prompt", system_prompt]
        if self._settings.CLAUDE_CLI_MODEL:
            argv += ["--model", self._settings.CLAUDE_CLI_MODEL]

        _logger.info(
            "claude_cli.request",
            session_id=session_id,
            model=self._settings.CLAUDE_CLI_MODEL,
            history_len=len(history),
            prompt=prompt,
            system_prompt_chars=len(system_prompt),
            has_schema=schema is not None,
        )

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self._settings.CLAUDE_CLI_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise LLMClientError(
                f"claude CLI timed out after {self._settings.CLAUDE_CLI_TIMEOUT_SECONDS}s"
            ) from exc
        except FileNotFoundError as exc:
            raise LLMClientError(
                f"claude CLI binary not found: {self._settings.CLAUDE_CLI_BIN!r}"
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        if proc.returncode != 0:
            raise LLMClientError(
                f"claude CLI exited with code {proc.returncode}: "
                f"{stderr_bytes.decode('utf-8', errors='replace')[:500]}"
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raw, tokens_in, tokens_out, is_error = self._extract_result(stdout)

        _logger.info(
            "claude_cli.response",
            session_id=session_id,
            latency_ms=round(latency_ms, 2),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            is_error=is_error,
            result=raw,
            stderr=stderr if stderr.strip() else None,
        )

        if is_error:
            raise LLMClientError(f"claude CLI reported error: {raw[:500]}")

        log_llm_call(
            session_id=session_id,
            messages=messages,
            raw_response=raw,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        return raw

    @staticmethod
    def _extract_result(
        stdout: str,
    ) -> tuple[str, int | None, int | None, bool]:
        """Pull ``result`` text + token counts out of ``claude -p --output-format json``.

        Returns ``(text, tokens_in, tokens_out, is_error)``. Falls back to the
        raw stdout if the wrapper JSON cannot be decoded — the response-parser
        layer will surface a friendly error then.
        """

        text = stdout.strip()
        if not text:
            return "", None, None, False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text, None, None, False
        if not isinstance(payload, dict):
            return text, None, None, False

        result = payload.get("result")
        if not isinstance(result, str):
            result = text
        result = _strip_code_fence(result)

        tokens_in: int | None = None
        tokens_out: int | None = None
        usage = payload.get("usage")
        if isinstance(usage, dict):
            for key in ("input_tokens", "prompt_tokens"):
                val = usage.get(key)
                if isinstance(val, int):
                    tokens_in = val
                    break
            for key in ("output_tokens", "completion_tokens"):
                val = usage.get(key)
                if isinstance(val, int):
                    tokens_out = val
                    break
        return result, tokens_in, tokens_out, bool(payload.get("is_error"))

    @staticmethod
    def _build_tools_system_addendum(tools: list[ToolDefinition]) -> str:
        """Build a system-prompt addendum describing the available tools.

        Asks Claude to emit a JSON object with a ``tool_calls`` array when it
        wants to invoke a tool, and plain text otherwise. The CLI doesn't
        expose Anthropic-format tool-calling on the command line for arbitrary
        tools, so we route through structured text instead.
        """

        tool_blocks: list[str] = []
        for tool in tools:
            schema = json.dumps(tool.parameters, ensure_ascii=False)
            tool_blocks.append(
                f"- name: {tool.name}\n"
                f"  description: {tool.description}\n"
                f"  parameters (JSON Schema): {schema}"
            )
        joined = "\n".join(tool_blocks)
        return (
            "\n\nYou have access to the following tools. To use one, respond "
            "with ONLY a JSON object on a single line, with no surrounding text "
            "and no markdown code fence:\n"
            '{"tool_calls": [{"id": "call_1", "name": "<name>", "arguments": {...}}]}\n'
            "You may include multiple entries in the ``tool_calls`` array to "
            "request several tool invocations at once. If you do NOT need a "
            "tool, respond with plain text (NO json wrapper).\n\n"
            f"Tools:\n{joined}"
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        if tools:
            addendum = self._build_tools_system_addendum(tools)
            augmented: list[dict[str, Any]] = []
            injected = False
            for msg in messages:
                if msg.get("role") == "system" and not injected:
                    augmented.append({**msg, "content": str(msg.get("content", "")) + addendum})
                    injected = True
                else:
                    augmented.append(msg)
            if not injected:
                augmented.insert(0, {"role": "system", "content": addendum.lstrip()})
        else:
            augmented = list(messages)

        raw = await self.chat(messages=augmented, session_id=session_id)

        if not tools:
            return LLMResponse(text=raw, tool_calls=[])

        stripped = _strip_code_fence(raw).strip()
        if not stripped.startswith("{"):
            return LLMResponse(text=raw, tool_calls=[])

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return LLMResponse(text=raw, tool_calls=[])

        if not isinstance(payload, dict) or "tool_calls" not in payload:
            return LLMResponse(text=raw, tool_calls=[])

        raw_calls = payload.get("tool_calls")
        if not isinstance(raw_calls, list):
            raise LLMClientError(
                f"Claude CLI returned malformed tool call: 'tool_calls' is not a list "
                f"({type(raw_calls).__name__})"
            )

        tool_calls: list[ToolCall] = []
        for entry in raw_calls:
            if not isinstance(entry, dict):
                raise LLMClientError(
                    f"Claude CLI returned malformed tool call: entry is not an object "
                    f"({type(entry).__name__})"
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise LLMClientError("Claude CLI returned malformed tool call: missing 'name'")
            arguments = entry.get("arguments", {})
            if not isinstance(arguments, dict):
                raise LLMClientError(
                    f"Claude CLI returned malformed tool call: 'arguments' is not an "
                    f"object ({type(arguments).__name__})"
                )
            raw_id = entry.get("id")
            call_id = raw_id if isinstance(raw_id, str) and raw_id else f"call_{uuid4().hex[:8]}"
            tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=cast(dict[str, Any], arguments),
                )
            )

        if not tool_calls:
            return LLMResponse(text=raw, tool_calls=[])

        return LLMResponse(text=None, tool_calls=tool_calls)
