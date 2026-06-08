"""Pure tests for the Bob history → SDK :class:`Chat` converter (PRD 0017 / M3).

The converter (:func:`bob.llm.lmstudio_sdk.history.messages_to_chat`) is pure —
no SDK server, no network — so these assert directly on the resulting ``Chat``'s
serialised history (``Chat._get_history()``), the deterministic dict the SDK
itself builds from the entries. Base roles only at issue 0111; tool-call
round-trips arrive in issue 0113.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bob.llm.lmstudio_sdk.history import HistoryConversionError, messages_to_chat
from bob.llm_client import _normalise_validator_role
from bob.validation.system_validator import (
    FALLBACK_VALIDATOR_PREFIX,
    SYSTEM_VALIDATOR_ROLE,
)


def _texts(chat_history: Any) -> list[tuple[str, str]]:
    """Flatten a ``Chat._get_history()`` dict into ``(role, joined_text)`` pairs."""

    pairs: list[tuple[str, str]] = []
    messages = chat_history["messages"]
    assert isinstance(messages, list)
    for entry in messages:
        assert isinstance(entry, dict)
        role = entry["role"]
        parts = entry.get("content", [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        pairs.append((str(role), text))
    return pairs


def test_system_only() -> None:
    chat = messages_to_chat([{"role": "system", "content": "You are Bob."}])
    assert _texts(chat._get_history()) == [("system", "You are Bob.")]


def test_multi_turn_user_assistant() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "bonjour"},
        {"role": "assistant", "content": "salut"},
        {"role": "user", "content": "ça va ?"},
    ]
    chat = messages_to_chat(messages)
    assert _texts(chat._get_history()) == [
        ("system", "sys"),
        ("user", "bonjour"),
        ("assistant", "salut"),
        ("user", "ça va ?"),
    ]


def test_validator_fold_applied_before_conversion() -> None:
    """The caller folds ``system_validator`` → prefixed ``system`` first.

    The converter only sees standard roles; this test wires the same fold the
    client applies (``_normalise_validator_role``) and asserts the validator row
    becomes a prefixed ``system`` segment. The SDK rejects consecutive system
    prompts, so the base + validator rows are coalesced into ONE system prompt
    (joined with a blank line) — the validator prefix stays distinguishable.
    """

    raw = [
        {"role": "system", "content": "base"},
        {"role": SYSTEM_VALIDATOR_ROLE, "content": "obey the schema"},
        {"role": "user", "content": "go"},
    ]
    folded = _normalise_validator_role(raw, allow_arbitrary_roles=False)
    chat = messages_to_chat(folded)
    assert _texts(chat._get_history()) == [
        ("system", "base\n\n" + FALLBACK_VALIDATOR_PREFIX + "obey the schema"),
        ("user", "go"),
    ]


def test_consecutive_system_messages_coalesced() -> None:
    """Consecutive system rows merge into one prompt (SDK rejects multiple)."""

    chat = messages_to_chat(
        [
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert _texts(chat._get_history()) == [("system", "a\n\nb"), ("user", "hi")]


def test_non_string_content_coerced() -> None:
    chat = messages_to_chat([{"role": "user", "content": 42}])
    assert _texts(chat._get_history()) == [("user", "42")]


def test_missing_content_becomes_empty() -> None:
    chat = messages_to_chat([{"role": "user"}])
    assert _texts(chat._get_history()) == [("user", "")]


def test_unknown_role_raises() -> None:
    # ``tool`` is now a supported role (issue 0113); a genuinely unknown role
    # still raises loudly.
    with pytest.raises(HistoryConversionError):
        messages_to_chat([{"role": "function", "content": "result"}])


# --- tool-turn round-trips (issue 0113 / M3) ---------------------------------


def _tool_parts(chat_history: Any) -> list[dict[str, Any]]:
    """Return the flat list of content parts across all messages, each tagged
    with its message role, for asserting tool-call / tool-result shapes."""

    parts: list[dict[str, Any]] = []
    for entry in chat_history["messages"]:
        for part in entry.get("content", []):
            if isinstance(part, dict):
                parts.append({"role": entry["role"], **part})
    return parts


def test_assistant_tool_calls_round_trip() -> None:
    """An assistant turn with OpenAI-style ``tool_calls`` becomes an SDK
    assistant response carrying ``toolCallRequest`` parts (args JSON-decoded)."""

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "search bitcoin"},
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"q": "bitcoin"}),
                    },
                }
            ],
        },
    ]
    history = messages_to_chat(messages)._get_history()
    parts = _tool_parts(history)
    request_parts = [p for p in parts if p["type"] == "toolCallRequest"]
    assert len(request_parts) == 1
    req = request_parts[0]["toolCallRequest"]
    assert req["name"] == "web_search"
    assert req["id"] == "call_1"
    # The OpenAI ``arguments`` JSON string is decoded back to a mapping.
    assert req["arguments"] == {"q": "bitcoin"}
    # The assistant text is preserved alongside the request.
    assert ("assistant", "on it") in _texts(history)


def test_tool_result_round_trip() -> None:
    """A ``tool`` result turn becomes an SDK ``toolCallResult`` part keyed by id."""

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "search bitcoin"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "price is high"},
        {"role": "assistant", "content": "It is high."},
    ]
    history = messages_to_chat(messages)._get_history()
    roles = [entry["role"] for entry in history["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    result_parts = [p for p in _tool_parts(history) if p["type"] == "toolCallResult"]
    assert len(result_parts) == 1
    assert result_parts[0]["content"] == "price is high"
    assert result_parts[0]["toolCallId"] == "call_1"


def test_assistant_without_tool_calls_unchanged() -> None:
    """An assistant turn with no ``tool_calls`` stays a plain text response
    (the base-layer arm is untouched)."""

    history = messages_to_chat(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )._get_history()
    parts = _tool_parts(history)
    assert all(p["type"] == "text" for p in parts)
    assert _texts(history) == [("user", "hi"), ("assistant", "hello")]
