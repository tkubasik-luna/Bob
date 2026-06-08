"""Bob message history → ``lmstudio`` SDK :class:`Chat` converter (PRD 0017 / M3).

A deep, PURE, separately-testable function: Bob's wire history (a list of
``{"role", "content"}`` dicts) is rebuilt into the SDK's :class:`lmstudio.Chat`
object the inference calls consume. This is the single place that knows how a
Bob role maps onto a ``Chat`` entry.

Scope of issue 0111 — base roles only:

- ``system`` → :meth:`Chat.add_system_prompt`
- ``user`` → :meth:`Chat.add_user_message`
- ``assistant`` → :meth:`Chat.add_assistant_response`

The ``system_validator`` role fold (issue 0048) is applied by the CALLER
(:class:`bob.llm.lmstudio_sdk.client.LMStudioSDKClient`) *before* conversion,
exactly like the OpenAI client folds it before dispatch — so by the time a
message reaches this converter every role is already one of the standard four.

Assistant-with-tool_calls turns and ``tool``-result turns round-trip through
:meth:`Chat.add_assistant_response` (with tool-call requests) and
:meth:`Chat.add_tool_result` respectively (EXTENDED in issue 0113). The dispatch
is a per-role branch keyed on ``role``: 0113 adds the ``tool`` branch and the
tool-call arm of the ``assistant`` branch without touching the existing arms.

Tool-turn wire shape (issue 0113)
---------------------------------
Bob carries the OpenAI-compatible wire history shape on both transports:

- an assistant turn that called tools is ``{"role": "assistant", "content": …,
  "tool_calls": [{"id", "type": "function", "function": {"name", "arguments"}}]}``
  where ``arguments`` is a JSON *string* (the OpenAI serialisation);
- a tool result is ``{"role": "tool", "tool_call_id": <id>, "content": <text>}``.

This converter maps those onto the SDK's
:meth:`Chat.add_assistant_response(text, [ToolCallRequest, …])` and
:meth:`Chat.add_tool_result({"type": "toolCallResult", "content", "toolCallId"})`
so a prior tool round-trip is faithfully replayed into the SDK ``Chat`` before
the next prediction.
"""

from __future__ import annotations

import json
from typing import Any

from lmstudio import Chat
from lmstudio._sdk_models import ToolCallRequest

#: The roles this converter understands. The base layer (issue 0111) handles
#: ``system`` / ``user`` / ``assistant``; issue 0113 adds ``tool`` (tool results)
#: and the tool-call arm of ``assistant``. An unexpected role here is a contract
#: bug surfaced loudly rather than dropped.
_BASE_ROLES = frozenset({"system", "user", "assistant", "tool"})


class HistoryConversionError(ValueError):
    """A Bob message could not be mapped onto an SDK :class:`Chat` entry.

    Raised for an unknown / non-standard role reaching the converter (the
    validator fold + standard-role assertion run upstream, so this only fires on
    a genuine programming error or a role the base layer does not yet support).
    """


def _content_str(message: dict[str, Any]) -> str:
    """Read a message's textual content as a plain string.

    Bob's base-role messages carry a string ``content``. A non-string (or
    missing) value is coerced via ``str`` so a malformed row never crashes the
    converter — mirrors :func:`bob.llm_client._estimate_tokens`'s tolerance.
    """

    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def _tool_call_requests(message: dict[str, Any]) -> list[ToolCallRequest]:
    """Build SDK :class:`ToolCallRequest`s from an assistant turn's ``tool_calls``.

    Reads the OpenAI-compatible ``tool_calls`` list Bob carries on an assistant
    message (``[{"id", "function": {"name", "arguments"}}]``). The ``arguments``
    field is a JSON *string* (OpenAI serialisation); it is decoded to a mapping
    here so the SDK round-trips the same structured args. A malformed / empty
    arguments string degrades to ``{}`` rather than raising — this is *history*
    replay (a past turn the model already produced), not a fresh capture, so it
    must never block the next prediction; the strict malformed-args guard lives
    on the live capture path (:mod:`bob.llm.lmstudio_sdk.tools`).
    """

    raw_calls = message.get("tool_calls") or []
    requests: list[ToolCallRequest] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = function.get("name") or raw.get("name") or ""
        arguments_raw = function.get("arguments", raw.get("arguments"))
        arguments: dict[str, Any]
        if isinstance(arguments_raw, dict):
            arguments = arguments_raw
        elif isinstance(arguments_raw, str) and arguments_raw.strip():
            try:
                decoded = json.loads(arguments_raw)
            except (TypeError, ValueError):
                decoded = {}
            arguments = decoded if isinstance(decoded, dict) else {}
        else:
            arguments = {}
        requests.append(
            ToolCallRequest(
                type="function",
                name=str(name),
                id=raw.get("id"),
                arguments=arguments,
            )
        )
    return requests


#: Separator used to coalesce consecutive ``system`` messages into one SDK
#: system prompt. Matches :meth:`bob.llm_client.ClaudeCliClient._split_messages`'s
#: ``"\n\n".join`` of system parts so the merged prompt reads identically across
#: transports.
_SYSTEM_JOIN = "\n\n"


def messages_to_chat(messages: list[dict[str, Any]]) -> Chat:
    """Convert Bob's wire ``messages`` into an SDK :class:`Chat`.

    ``messages`` is the validator-folded, standard-role history (see module
    docstring). Returns a fresh :class:`Chat` with one entry per message, in
    order. Raises :class:`HistoryConversionError` on a role the base layer does
    not handle.

    DEVIATION from the OpenAI transport (robustness): the SDK :class:`Chat`
    REJECTS multi-part / consecutive system prompts
    (``LMStudioRuntimeError: Multi-part or consecutive system prompts are not
    supported``), whereas the OpenAI-compatible endpoint accepts any number of
    ``system`` rows. Bob routinely emits several (a base system prompt + the
    issue-0048 folded ``system_validator`` row). To preserve behaviour we
    coalesce a RUN of consecutive ``system`` messages into a single system prompt
    joined with :data:`_SYSTEM_JOIN` (the same join the Claude CLI client uses),
    rather than letting the SDK raise.
    """

    chat = Chat()
    pending_system: list[str] = []

    def flush_system() -> None:
        if pending_system:
            chat.add_system_prompt(_SYSTEM_JOIN.join(pending_system))
            pending_system.clear()

    for index, message in enumerate(messages):
        role = message.get("role")
        if role not in _BASE_ROLES:
            raise HistoryConversionError(
                f"Cannot convert role {role!r} at messages[{index}] to an SDK Chat "
                f"entry. Base layer supports {sorted(_BASE_ROLES)}; tool-call "
                f"round-trips arrive in issue 0113."
            )
        content = _content_str(message)
        if role == "system":
            pending_system.append(content)
            continue
        # A non-system message closes any open run of system prompts first.
        flush_system()
        if role == "user":
            chat.add_user_message(content)
        elif role == "tool":
            # A ``tool`` result turn → one SDK ``toolCallResult`` part, keyed by
            # the originating call id so the model pairs it with its request.
            chat.add_tool_result(
                {
                    "type": "toolCallResult",
                    "content": content,
                    "toolCallId": message.get("tool_call_id"),
                }
            )
        else:  # role == "assistant"
            requests = _tool_call_requests(message)
            if requests:
                # An assistant turn that called tools: text (may be empty) +
                # one tool-call-request part per call (issue 0113).
                chat.add_assistant_response(content, requests)
            else:
                chat.add_assistant_response(content)
    flush_system()
    return chat


__all__ = ["HistoryConversionError", "messages_to_chat"]
