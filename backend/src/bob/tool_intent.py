"""Tool-intent gate for the speculative Draft — PRD 0016 / issue 0104.

The Draft speculates ONLY the conversational reply: a turn that will dispatch a
tool must stay COLD, because the raw draft text can never spawn a sub-task —
adopting it would SKIP the tool dispatch and speak a hallucinated answer
instead ("quelle est la météo ?" answered from the mini model's imagination).
:class:`bob.speculative_draft.SpeculativeDraft` accepts an ``is_tool_intent``
predicate for exactly this boundary; this module builds the production one.

V1 reuses the SAME pure lexical scorer as the ``select_tools`` advertisement
gate (PRD 0015 / issue 0092): the partial transcript is scored against the live
sub-agent tool registry (gmail, web search, the MCP fleet…) and the turn is
classified a TOOL turn when any tool clears ``TOOL_RETRIEVAL_MIN_SCORE``. Same
knob, same tokenisation — "would a sub-task surface this tool?" and "must the
Draft stay cold?" answer from one source of truth, and a registry tag added for
retrieval (e.g. ``météo`` on ``web_search``) hardens both gates at once.

Asymmetric by design: a false POSITIVE (tool intent flagged on a chat turn)
only costs the anticipation — the turn runs cold, which is always correct, just
slower. A false NEGATIVE risks a committed draft replacing a tool dispatch. So
the threshold stays at the retrieval gate's low default rather than growing its
own stricter knob.

The predicate closes over the registry OBJECT and iterates it at call time, so
tools registered after construction (the MCP fleet lands during boot) are seen
live — no rebuild needed.
"""

from __future__ import annotations

from bob.speculative_draft import ToolIntentPredicate
from bob.sub_agent.tool_retrieval import score_tools


def build_tool_intent_predicate(registry: object, *, min_score: int) -> ToolIntentPredicate:
    """Build the pure ``is_tool_intent`` predicate over ``registry``.

    ``registry`` is any iterable of
    :class:`bob.sub_agent.tool_registry.SubAgentToolDefinition` (duck-typed like
    :func:`bob.sub_agent.tool_retrieval.select_tools`). ``min_score`` is the
    minimum lexical score (``TOOL_RETRIEVAL_MIN_SCORE``) at which a tool hit
    classifies the turn as a TOOL turn; it is floored at 1 so a zero/negative
    knob can never flag every turn (every tool scores ≥ 0 on any text).

    The returned callable is pure + synchronous (the
    :data:`~bob.speculative_draft.ToolIntentPredicate` contract): no I/O, no
    mutation — safe to call per ``stt_partial``.
    """

    floor = max(1, min_score)

    def is_tool_intent(partial_text: str) -> bool:
        text = partial_text.strip()
        if not text:
            return False
        scored = score_tools(registry, text)
        # ``score_tools`` sorts descending — the head carries the max score.
        return bool(scored) and scored[0][1] >= floor

    return is_tool_intent


__all__ = ["build_tool_intent_predicate"]
