"""Backend capability descriptor + codec selection (PRD 0008 / issue 0058).

The codec layer's whole point is "core owns the loop, codec owns the format":
the call site declares *what a backend can do* (a :class:`BackendCapability`)
and asks :func:`select_codec` for the most robust codec that backend supports.
No per-call ``if backend == ...`` branching survives at the call site.

Today only the native function-calling codec exists
(:class:`bob.llm.tooling.codec.NativeToolCodec`). The guided-JSON codec
(issue 0060) and the Hermes-tag codec (issue 0061) are *declared* in the
:data:`ToolMode` literal and in :class:`BackendCapability` so the selection
logic can already express the preference order, but selecting them raises a
clear :class:`CodecNotAvailableError` until those issues land. That keeps the
seam honest (no dead code, no silent fallback to native when guided/hermes was
explicitly requested) while leaving an obvious extension point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover — avoid an import cycle at runtime.
    from bob.llm.tooling.codec import ToolCodec

#: User-facing override for which wire format to use, mirrored by the
#: ``LLM_TOOL_MODE`` setting. ``auto`` (the default) defers to the backend's
#: declared :class:`BackendCapability`; the explicit modes force one codec and
#: raise if the backend does not support it (so a misconfiguration is loud, not
#: a silent degrade).
ToolMode = Literal["auto", "native", "guided", "hermes"]


class CodecNotAvailableError(RuntimeError):
    """Raised when no codec can satisfy the requested mode + capability.

    Covers two cases:

    - an explicit ``LLM_TOOL_MODE`` asks for a codec the backend does not
      declare support for (e.g. ``hermes`` against a native-only backend), or
    - ``auto`` against a backend that declares no supported format at all.

    Also raised by the guided / Hermes branches until issues 0060 / 0061 land
    — the modes are declarable today but not yet implemented.
    """


@dataclass(frozen=True)
class BackendCapability:
    """What tool-calling wire formats a given provider/model supports.

    One descriptor per backend (LM Studio, Claude CLI, …). The booleans are
    deliberately independent — a backend may support several formats, and
    :func:`select_codec` picks the most robust *supported* one. Defaults are
    conservative (everything off) so a new backend must opt in explicitly.

    - ``native_function_calling`` — the OpenAI ``tools=[]`` /
      ``message.tool_calls`` surface. The most robust path when the provider
      implements it reliably (LM Studio does).
    - ``guided_json`` — the provider can be constrained to emit a JSON object
      matching a schema (vLLM / llama.cpp grammar). Codec lands in issue 0060.
    - ``hermes_tags`` — the model was trained on Hermes-style
      ``<tool_call>…</tool_call>`` tags. Codec lands in issue 0061.
    """

    native_function_calling: bool = False
    guided_json: bool = False
    hermes_tags: bool = False


#: Per-backend capability defaults keyed by the ``LLM_PROVIDER`` /
#: ``JARVIS_BACKEND`` / ``SUBAGENT_BACKEND`` string. LM Studio (and any
#: OpenAI-compatible endpoint Bob points it at) exposes reliable native
#: function calling — that is the path :class:`bob.llm_client.LMStudioClient`
#: drives today. The Claude CLI has NO native tool-calling on the command line
#: (it hand-writes a ``{"tool_calls":[…]}`` JSON blob in the system prompt);
#: that prompt-based path is its own thing today and is slated to become the
#: Hermes codec in issue 0061, so we declare ``hermes_tags`` for it now but the
#: CLI client keeps its current bespoke path untouched in this issue.
_BACKEND_CAPABILITIES: dict[str, BackendCapability] = {
    "lm_studio": BackendCapability(native_function_calling=True),
    "claude_cli": BackendCapability(hermes_tags=True),
}


def capability_for_backend(backend: str) -> BackendCapability:
    """Return the declared :class:`BackendCapability` for ``backend``.

    Unknown backends get the conservative all-off default so
    :func:`select_codec` raises a clear :class:`CodecNotAvailableError` rather
    than guessing — a new backend must register its capabilities here.
    """

    return _BACKEND_CAPABILITIES.get(backend, BackendCapability())


def select_codec(capability: BackendCapability, mode: ToolMode = "auto") -> ToolCodec:
    """Pick the most robust supported :class:`ToolCodec` for ``capability``.

    Selection rules:

    - ``mode="auto"`` (default): prefer native function calling, then guided
      JSON, then Hermes tags — most-robust-first. Raise
      :class:`CodecNotAvailableError` if the backend declares no supported
      format.
    - ``mode="native"|"guided"|"hermes"``: force that codec, but only if the
      capability declares support for it; otherwise raise
      :class:`CodecNotAvailableError` so the misconfiguration is loud.

    Only :class:`bob.llm.tooling.codec.NativeToolCodec` exists in issue 0058.
    The guided / Hermes branches raise until issues 0060 / 0061 implement them
    — they are real (reachable) extension points, not dead code.
    """

    # Local import avoids a module-import cycle: ``codec`` imports ``spec``,
    # and a future codec may want the capability types.
    from bob.llm.tooling.codec import NativeToolCodec

    if mode == "native":
        if not capability.native_function_calling:
            raise CodecNotAvailableError(
                "LLM_TOOL_MODE=native but the backend does not declare "
                "native_function_calling support."
            )
        return NativeToolCodec()
    if mode == "guided":
        if not capability.guided_json:
            raise CodecNotAvailableError(
                "LLM_TOOL_MODE=guided but the backend does not declare guided_json support."
            )
        raise CodecNotAvailableError(
            "Guided-JSON codec is not implemented yet (lands in issue 0060)."
        )
    if mode == "hermes":
        if not capability.hermes_tags:
            raise CodecNotAvailableError(
                "LLM_TOOL_MODE=hermes but the backend does not declare hermes_tags support."
            )
        raise CodecNotAvailableError(
            "Hermes-tag codec is not implemented yet (lands in issue 0061)."
        )

    # mode == "auto": most-robust-first.
    if capability.native_function_calling:
        return NativeToolCodec()
    if capability.guided_json:
        raise CodecNotAvailableError(
            "Backend declares guided_json but the guided-JSON codec is not "
            "implemented yet (lands in issue 0060)."
        )
    if capability.hermes_tags:
        raise CodecNotAvailableError(
            "Backend declares hermes_tags but the Hermes-tag codec is not "
            "implemented yet (lands in issue 0061)."
        )
    raise CodecNotAvailableError(
        "No tool-calling codec available: the backend declares no supported "
        "wire format (native_function_calling / guided_json / hermes_tags all False)."
    )


__all__ = [
    "BackendCapability",
    "CodecNotAvailableError",
    "ToolMode",
    "capability_for_backend",
    "select_codec",
]
