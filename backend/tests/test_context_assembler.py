"""Tests for :class:`bob.context.assembler.ContextAssembler`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from bob.context.assembler import ContextAssembler, ContextAssemblerError
from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.policy import legacy_full_history_policy, parse_policy_overrides
from bob.context.provider import AssemblyContext


class _StaticProvider:
    """Test double — returns a pre-baked list of entries each call.

    Each call increments ``call_count`` so the test can assert the assembler
    actually invoked the provider (and once per assembly).
    """

    def __init__(self, provider_id: str, entries: Sequence[ContextEntry]) -> None:
        self._provider_id = provider_id
        self._entries = list(entries)
        self.calls: list[AssemblyContext] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        self.calls.append(ctx)
        return list(self._entries)


def _msg_entry(
    *,
    entry_id: str,
    role: str,
    content: str,
    kind: str = "user_turn",
    provider_id: str = "p",
) -> ContextEntry:
    return ContextEntry(
        id=entry_id,
        kind=kind,  # type: ignore[arg-type]
        source="test",
        token_estimate=0,
        pinned=False,
        created_at="",
        provider_id=provider_id,
        payload={"role": role, "content": content},
        schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
    )


def test_assemble_iterates_providers_in_policy_order() -> None:
    first = _StaticProvider(
        "p1",
        [_msg_entry(entry_id="p1:0", role="system", content="alpha", provider_id="p1")],
    )
    second = _StaticProvider(
        "p2",
        [
            _msg_entry(entry_id="p2:0", role="user", content="hi", provider_id="p2"),
            _msg_entry(
                entry_id="p2:1",
                role="assistant",
                content="hello",
                kind="assistant_turn",
                provider_id="p2",
            ),
        ],
    )
    policy = parse_policy_overrides(provider_ids=["p1", "p2"])

    assembler = ContextAssembler(providers=[first, second], policy=policy)
    messages = assembler.assemble()

    assert messages == [
        {"role": "system", "content": "alpha"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_assemble_orders_by_policy_provider_ids_not_constructor_order() -> None:
    """Construction order does not constrain the policy order."""

    a = _StaticProvider("a", [_msg_entry(entry_id="a:0", role="user", content="A")])
    b = _StaticProvider("b", [_msg_entry(entry_id="b:0", role="user", content="B")])
    policy = parse_policy_overrides(provider_ids=["b", "a"])

    assembler = ContextAssembler(providers=[a, b], policy=policy)
    messages = assembler.assemble()

    assert [m["content"] for m in messages] == ["B", "A"]


def test_assemble_raises_when_policy_references_unknown_provider() -> None:
    policy = parse_policy_overrides(provider_ids=["ghost"])
    assembler = ContextAssembler(providers=[], policy=policy)

    with pytest.raises(ContextAssemblerError):
        assembler.assemble()


def test_construction_rejects_duplicate_provider_ids() -> None:
    a = _StaticProvider("dup", [])
    b = _StaticProvider("dup", [])
    with pytest.raises(ValueError):
        ContextAssembler(providers=[a, b], policy=legacy_full_history_policy())


def test_assemble_propagates_user_message_to_provider_context() -> None:
    """``user_message`` reaches the provider through :class:`AssemblyContext`."""

    captured: list[str | None] = []

    class _Capturing:
        provider_id = "cap"

        def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
            captured.append(ctx.user_message)
            return ()

    policy = parse_policy_overrides(provider_ids=["cap"])
    assembler = ContextAssembler(providers=[_Capturing()], policy=policy)
    assembler.assemble(user_message="ping")

    assert captured == ["ping"]


def test_assemble_rejects_payload_without_role() -> None:
    bad_entry = ContextEntry(
        id="bad",
        kind="user_turn",
        source="t",
        token_estimate=0,
        pinned=False,
        created_at="",
        provider_id="p",
        payload={"content": "missing role"},
    )
    provider = _StaticProvider("p", [bad_entry])
    policy = parse_policy_overrides(provider_ids=["p"])
    assembler = ContextAssembler(providers=[provider], policy=policy)

    with pytest.raises(ContextAssemblerError):
        assembler.assemble()


def test_assemble_rejects_payload_without_string_content() -> None:
    bad_entry = ContextEntry(
        id="bad",
        kind="user_turn",
        source="t",
        token_estimate=0,
        pinned=False,
        created_at="",
        provider_id="p",
        payload={"role": "user", "content": 123},
    )
    provider = _StaticProvider("p", [bad_entry])
    policy = parse_policy_overrides(provider_ids=["p"])
    assembler = ContextAssembler(providers=[provider], policy=policy)

    with pytest.raises(ContextAssemblerError):
        assembler.assemble()


def test_collect_entries_does_not_project_payload() -> None:
    """``collect_entries`` returns the raw ContextEntry list."""

    provider = _StaticProvider("p", [_msg_entry(entry_id="p:0", role="user", content="hi")])
    policy = parse_policy_overrides(provider_ids=["p"])
    assembler = ContextAssembler(providers=[provider], policy=policy)

    ctx = AssemblyContext(policy=policy, user_message=None)
    entries = assembler.collect_entries(ctx)
    assert len(entries) == 1
    assert entries[0].payload == {"role": "user", "content": "hi"}


def test_policy_is_exposed_via_property() -> None:
    policy = parse_policy_overrides(policy_id="x", provider_ids=["p"])
    assembler = ContextAssembler(
        providers=[_StaticProvider("p", [])],
        policy=policy,
    )
    assert assembler.policy is policy


def test_assemble_is_idempotent() -> None:
    """The assembler must not retain state between calls."""

    provider = _StaticProvider(
        "p",
        [
            _msg_entry(entry_id="p:0", role="user", content="hi"),
            _msg_entry(
                entry_id="p:1",
                role="assistant",
                content="hello",
                kind="assistant_turn",
            ),
        ],
    )
    policy = parse_policy_overrides(provider_ids=["p"])
    assembler = ContextAssembler(providers=[provider], policy=policy)

    first = assembler.assemble()
    second = assembler.assemble()

    assert first == second


def test_assembly_context_round_trips_policy() -> None:
    """``AssemblyContext.policy`` matches the assembler's policy."""

    seen: list[Any] = []

    class _PolicyEcho:
        provider_id = "echo"

        def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
            seen.append(ctx.policy)
            return ()

    policy = replace(legacy_full_history_policy(), token_budget=42)
    assembler = ContextAssembler(
        providers=[_PolicyEcho()],
        policy=parse_policy_overrides(provider_ids=["echo"], token_budget=42),
    )
    assembler.assemble()

    assert seen and seen[0].token_budget == 42
    assert policy.token_budget == 42
