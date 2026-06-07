"""Deterministic scriptable LLM backend for the attestation harness.

PRD 0016 / issue 0098. :class:`FakeLlmClient` is a :class:`bob.llm_client.LLMClient`
implementation that NEVER touches the network: every reply is decided locally
from a scenario-supplied script. It is the default backend of ``bob attest``
(``llm: fake``) so a scenario runs offline, deterministically, in CI.

Why mirror the SDK-fake pattern? :mod:`tests.test_lm_studio_manager` fakes the
``lmstudio`` SDK at the client-factory boundary; we do the analogue one layer
up — fake the whole :class:`LLMClient` and plug it into the provider switch in
:mod:`bob.llm.factory`. The orchestrator, sub-agent runner and WS layer all stay
byte-for-byte unchanged: they just receive a client whose replies are scripted.

Scripting model (Annexe C ``fake_llm``)
---------------------------------------

The scenario carries a list of rules::

    fake_llm:
      - role: jarvis
        on_input_contains: "météo"
        reply: "Il fait beau aujourd'hui à Paris."

Each rule is a :class:`FakeRule`. At call time the client takes the LAST user
message's content and returns the ``reply`` of the FIRST rule whose
``on_input_contains`` is a substring of it (case-insensitive) AND whose ``role``
matches the client's role (a rule with no ``role`` matches any role; a client
with no role only filters on the text). When no rule matches, a generic
deterministic line is returned so a turn always produces a non-empty ``say`` —
the harness can still attest the *path* even for an unscripted input.

Every reply is delivered as a ``say`` tool call (the unified Jarvis emission,
PRD 0006). The orchestrator drives :meth:`stream_complete`; the base-class
fallback synthesises the streamed tool-call trio from :meth:`complete`, so we
only implement :meth:`complete` (and :meth:`chat` for the structured-reply
paths). ``temperature`` does not apply — the output is fully determined by the
script, which is exactly the determinism the attestation contract wants.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import LLMClient

#: Reply used when no scripted rule matches the input. Deterministic + non-empty
#: so ``deliverable_nonempty`` / ``event_emitted`` still hold for unscripted
#: turns — the harness attests the path, not the wording.
DEFAULT_REPLY = "D'accord."


@dataclass(frozen=True)
class FakeRule:
    """One scripted reply rule (an Annexe C ``fake_llm`` entry).

    - ``reply`` — the text the fake speaks (becomes ``say.speech``).
    - ``on_input_contains`` — case-insensitive substring the last user
      message must contain for this rule to fire. Empty string matches any
      input (an unconditional catch-all).
    - ``role`` — restrict the rule to a client wired for this role
      (``jarvis`` / ``subagent`` / ...). ``None`` matches any role.
    """

    reply: str
    on_input_contains: str = ""
    role: str | None = None

    def matches(self, *, role: str | None, last_user_text: str) -> bool:
        """Return whether this rule fires for the given role + input text."""

        if self.role is not None and role is not None and self.role != role:
            return False
        if not self.on_input_contains:
            return True
        return self.on_input_contains.casefold() in last_user_text.casefold()


@dataclass(frozen=True)
class FakeScript:
    """An ordered list of :class:`FakeRule` driving a :class:`FakeLlmClient`.

    Construct from the scenario's ``fake_llm`` list via :meth:`from_rules` or
    from the JSON env payload via :meth:`from_json`. The two share one parser
    so the in-process unit tests and the subprocess boot path cannot drift.
    """

    rules: tuple[FakeRule, ...] = field(default_factory=tuple)

    @classmethod
    def from_rules(cls, raw: object) -> FakeScript:
        """Parse a list of rule dicts (defensive — bad entries are skipped).

        Accepts the exact ``fake_llm`` shape from the YAML scenario: a list of
        ``{"role", "on_input_contains", "reply"}`` dicts. A non-list, or an
        entry missing a usable ``reply``, is dropped rather than raising so a
        slightly malformed scenario still boots a (less useful) fake rather
        than crashing the whole attestation.
        """

        if not isinstance(raw, list):
            return cls(rules=())
        parsed: list[FakeRule] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            reply = entry.get("reply")
            if not isinstance(reply, str) or not reply:
                continue
            on_input = entry.get("on_input_contains")
            role = entry.get("role")
            parsed.append(
                FakeRule(
                    reply=reply,
                    on_input_contains=on_input if isinstance(on_input, str) else "",
                    role=role if isinstance(role, str) and role else None,
                )
            )
        return cls(rules=tuple(parsed))

    @classmethod
    def from_json(cls, payload: str) -> FakeScript:
        """Parse the JSON string carried by ``BOB_FAKE_LLM_SCRIPT``.

        Empty / blank / undecodable payloads yield an empty script (the client
        then always returns :data:`DEFAULT_REPLY`).
        """

        if not payload or not payload.strip():
            return cls(rules=())
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return cls(rules=())
        return cls.from_rules(data)

    def to_json(self) -> str:
        """Serialise back to the ``BOB_FAKE_LLM_SCRIPT`` JSON shape."""

        return json.dumps(
            [
                {
                    "role": rule.role,
                    "on_input_contains": rule.on_input_contains,
                    "reply": rule.reply,
                }
                for rule in self.rules
            ],
            ensure_ascii=False,
        )

    def reply_for(self, *, role: str | None, last_user_text: str) -> str:
        """Return the reply for the first matching rule, else the default."""

        for rule in self.rules:
            if rule.matches(role=role, last_user_text=last_user_text):
                return rule.reply
        return DEFAULT_REPLY


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Extract the content of the last ``user`` message (empty if none)."""

    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if content is not None:
                return str(content)
            return ""
    return ""


class FakeLlmClient(LLMClient):
    """Scriptable, offline :class:`LLMClient` for ``bob attest``.

    The ``role`` records which orchestrator role this instance serves
    (``jarvis`` / ``subagent``) so role-scoped rules can target it; it also
    backs the future ``role_used_model`` assertion (a later slice records the
    role→model mapping — the seam exists here today).

    :meth:`complete` always answers with a single ``say`` tool call carrying
    the scripted speech, so the orchestrator's unified-emission path runs end
    to end. :meth:`chat` returns the raw speech string for any structured-reply
    call site. :meth:`stream_complete` inherits the base-class fallback, which
    replays :meth:`complete` as the streamed tool-call trio the orchestrator
    consumes — no per-tick scripting needed for the skeleton.
    """

    def __init__(self, script: FakeScript, *, role: str | None = None) -> None:
        self._script = script
        self._role = role
        #: Recorded per-call inputs — lets in-process tests assert determinism
        #: and lets future assertions (role_used_model) inspect usage.
        self.calls: list[dict[str, Any]] = []

    @property
    def role(self) -> str | None:
        return self._role

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        text = _last_user_text(messages)
        self.calls.append({"kind": "chat", "last_user_text": text, "schema": schema})
        return self._script.reply_for(role=self._role, last_user_text=text)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        text = _last_user_text(messages)
        self.calls.append({"kind": "complete", "last_user_text": text, "tools": tools})
        speech = self._script.reply_for(role=self._role, last_user_text=text)
        # Unified emission: every Jarvis reply is a ``say`` tool call (PRD 0006).
        # ``ui`` is omitted (a plain spoken reply) — the skeleton only needs to
        # drive the speech path; richer UI payloads are a later slice's concern.
        call = ToolCall(id=f"call_{uuid4().hex[:8]}", name="say", arguments={"speech": speech})
        return LLMResponse(text=None, tool_calls=[call])


def build_fake_client_from_settings(role: str | None = None) -> FakeLlmClient:
    """Build a :class:`FakeLlmClient` from the active settings' env payload.

    Reads :attr:`bob.config.Settings.BOB_FAKE_LLM_SCRIPT` (the JSON the
    ``bob attest`` CLI injects before booting the subprocess) and decodes it
    into a :class:`FakeScript`. Used by the ``fake`` branch of
    :func:`bob.llm.factory._build_for_backend`.
    """

    from bob.config import get_settings

    script = FakeScript.from_json(get_settings().BOB_FAKE_LLM_SCRIPT)
    return FakeLlmClient(script, role=role)
