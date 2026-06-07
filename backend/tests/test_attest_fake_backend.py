"""Unit tests for the attestation fake LLM backend (issue 0098).

Covers the deterministic scripted-reply contract: role + ``on_input_contains``
matching, the unconditional + default fallbacks, JSON round-trip of the script,
and that a reply is delivered as a ``say`` tool call so the orchestrator's
unified-emission path runs.
"""

from __future__ import annotations

import pytest

from bob.attest.fake_backend import (
    DEFAULT_REPLY,
    FakeLlmClient,
    FakeRule,
    FakeScript,
)


def _script(*rules: dict[str, object]) -> FakeScript:
    return FakeScript.from_rules(list(rules))


async def test_complete_returns_say_tool_call_with_scripted_speech() -> None:
    client = FakeLlmClient(
        _script({"role": "jarvis", "on_input_contains": "météo", "reply": "Il fait beau."}),
        role="jarvis",
    )
    resp = await client.complete([{"role": "user", "content": "quelle météo à Paris"}])

    assert resp.text is None
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "say"
    assert call.arguments == {"speech": "Il fait beau."}


async def test_complete_is_deterministic_across_calls() -> None:
    client = FakeLlmClient(_script({"on_input_contains": "ping", "reply": "pong"}), role="jarvis")
    first = await client.complete([{"role": "user", "content": "ping"}])
    second = await client.complete([{"role": "user", "content": "ping"}])
    assert first.tool_calls[0].arguments == second.tool_calls[0].arguments == {"speech": "pong"}


async def test_chat_returns_raw_scripted_string() -> None:
    client = FakeLlmClient(_script({"on_input_contains": "x", "reply": "hello"}))
    out = await client.chat([{"role": "user", "content": "say x please"}])
    assert out == "hello"


async def test_no_match_falls_back_to_default_reply() -> None:
    client = FakeLlmClient(_script({"on_input_contains": "zzz", "reply": "nope"}), role="jarvis")
    resp = await client.complete([{"role": "user", "content": "totally unrelated"}])
    assert resp.tool_calls[0].arguments == {"speech": DEFAULT_REPLY}


def test_first_matching_rule_wins() -> None:
    script = _script(
        {"on_input_contains": "bonjour", "reply": "premier"},
        {"on_input_contains": "bonjour", "reply": "second"},
    )
    assert script.reply_for(role=None, last_user_text="bonjour") == "premier"


def test_role_scoped_rule_skipped_for_other_role() -> None:
    script = _script(
        {"role": "subagent", "on_input_contains": "x", "reply": "sub-only"},
        {"on_input_contains": "x", "reply": "catch-all"},
    )
    # A jarvis client must NOT match the subagent-scoped rule; it falls through
    # to the unscoped catch-all.
    assert script.reply_for(role="jarvis", last_user_text="x here") == "catch-all"
    # The subagent client matches the scoped rule first.
    assert script.reply_for(role="subagent", last_user_text="x here") == "sub-only"


def test_empty_on_input_contains_is_unconditional_catch_all() -> None:
    script = _script({"reply": "always"})
    assert script.reply_for(role="jarvis", last_user_text="anything at all") == "always"


def test_matching_is_case_insensitive() -> None:
    rule = FakeRule(reply="r", on_input_contains="MÉTÉO")
    assert rule.matches(role=None, last_user_text="quelle météo")


def test_from_rules_drops_malformed_entries() -> None:
    script = FakeScript.from_rules(
        [
            {"reply": "ok"},
            {"no_reply_key": True},  # dropped — no reply
            "not a dict",  # dropped
            {"reply": ""},  # dropped — empty reply
            42,  # dropped
        ]
    )
    assert [r.reply for r in script.rules] == ["ok"]


def test_from_rules_non_list_yields_empty_script() -> None:
    assert FakeScript.from_rules({"role": "jarvis"}).rules == ()
    assert FakeScript.from_rules(None).rules == ()


def test_json_round_trip_preserves_rules() -> None:
    original = _script(
        {"role": "jarvis", "on_input_contains": "météo", "reply": "beau temps"},
        {"reply": "défaut"},
    )
    restored = FakeScript.from_json(original.to_json())
    assert restored == original


@pytest.mark.parametrize("payload", ["", "   ", "not json", "{bad"])
def test_from_json_tolerates_bad_payloads(payload: str) -> None:
    assert FakeScript.from_json(payload).rules == ()


async def test_last_user_message_drives_match_not_earlier_ones() -> None:
    # Only the LAST user message should be matched (the live turn).
    client = FakeLlmClient(
        _script(
            {"on_input_contains": "first", "reply": "A"},
            {"on_input_contains": "second", "reply": "B"},
        )
    )
    resp = await client.complete(
        [
            {"role": "user", "content": "the first thing"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "now the second thing"},
        ]
    )
    assert resp.tool_calls[0].arguments == {"speech": "B"}


async def test_calls_are_recorded_for_introspection() -> None:
    client = FakeLlmClient(_script({"reply": "x"}), role="jarvis")
    await client.complete([{"role": "user", "content": "hi"}])
    await client.chat([{"role": "user", "content": "yo"}])
    assert [c["kind"] for c in client.calls] == ["complete", "chat"]
    assert client.calls[0]["last_user_text"] == "hi"
    assert client.role == "jarvis"
