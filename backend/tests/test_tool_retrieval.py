"""Unit tests for goal-driven tool retrieval — PRD 0015 / issue 0092.

:func:`bob.sub_agent.tool_retrieval.select_tools` is pure + deterministic, so
the tests are table-driven over hand-built registries: mail / web / multi-intent
/ junk goals each assert the exact advertised set AND ordering. The ``k`` cap,
``min_score`` threshold, the no-zero-score-pad rule, and the empty → ``always_on``
fallback are each pinned. A ~30-tool simulated registry asserts a focused goal
advertises ≤ ``k`` tools.
"""

from __future__ import annotations

from pydantic import BaseModel

from bob.sub_agent.tool_registry import (
    SubAgentToolDefinition,
    SubAgentToolHandlerContext,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
    build_default_subagent_registry,
)
from bob.sub_agent.tool_retrieval import score_tools, select_tools


class _Args(BaseModel):
    """Minimal args model — retrieval never touches it, only name/desc/tags."""

    query: str = ""


async def _noop_handler(
    _ctx: SubAgentToolHandlerContext, _args: BaseModel
) -> SubAgentToolHandlerOutcome:
    return SubAgentToolHandlerOutcome(status="ok")


def _tool(
    name: str,
    *,
    description: str = "",
    tags: tuple[str, ...] = (),
    always_on: bool = False,
) -> SubAgentToolDefinition:
    return SubAgentToolDefinition(
        name=name,
        version="v1",
        description=description,
        args_model=_Args,
        handler=_noop_handler,
        tags=tags,
        always_on=always_on,
    )


def _names(tools: list[SubAgentToolDefinition]) -> list[str]:
    return [t.name for t in tools]


# A small, realistic registry mirroring the production shape (gmail + web tools).
_GMAIL = _tool(
    "gmail_search",
    description="Recherche dans la boîte Gmail de l'utilisateur (expéditeur, sujet, dates).",
    tags=("mail", "email", "gmail", "boîte", "inbox", "courriel"),
)
_WEB_SEARCH = _tool(
    "web_search",
    description="Cherche le web et renvoie une liste de résultats.",
    tags=("web", "internet", "actu", "actualité", "news", "météo", "recherche"),
)
_WEB_FETCH = _tool(
    "web_fetch",
    description="Récupère le contenu textuel d'une URL pour analyse.",
    tags=("web", "page", "url", "article", "fetch"),
)


def _registry() -> SubAgentToolRegistry:
    return SubAgentToolRegistry([_GMAIL, _WEB_SEARCH, _WEB_FETCH])


# ---------------------------------------------------------------------------
# Table-driven intent routing — mail / web / multi-intent / junk.
# ---------------------------------------------------------------------------


def test_mail_goal_advertises_gmail_only() -> None:
    """Goal « dernier mail » advertises ``gmail_search`` and excludes web tools."""

    tools = select_tools(_registry(), "Trouve le dernier mail reçu", k=8, min_score=1)
    assert _names(tools) == ["gmail_search"]


def test_web_goal_advertises_web_search() -> None:
    """Goal « actu sur X » advertises ``web_search`` (an actualité/news intent)."""

    tools = select_tools(_registry(), "Donne-moi l'actu sur la réforme", k=8, min_score=1)
    # web_search carries the ``actualité`` tag; gmail_search scores 0 and is excluded.
    assert "web_search" in _names(tools)
    assert "gmail_search" not in _names(tools)


def test_multi_intent_goal_surfaces_both() -> None:
    """« mes mails et la météo » surfaces both gmail_search and web_search."""

    tools = select_tools(_registry(), "Montre mes mails et la météo", k=8, min_score=1)
    names = _names(tools)
    assert "gmail_search" in names
    assert "web_search" in names


def test_junk_goal_advertises_nothing_without_always_on() -> None:
    """A goal matching no tool token advertises NOTHING when no always_on core."""

    tools = select_tools(_registry(), "azerty qwerty zzz", k=8, min_score=1)
    assert tools == []


def test_ensure_non_empty_falls_back_on_junk_goal() -> None:
    """RC-A: ``ensure_non_empty`` never leaves the model tool-less.

    A goal scoring every tool zero would otherwise advertise nothing (the run
    then silently has no tools). With the safety net on, the full registry is
    surfaced (top-k by score → here all-zero → first k by ascending name).
    """

    tools = select_tools(_registry(), "azerty qwerty zzz", k=8, min_score=1, ensure_non_empty=True)
    assert _names(tools) == ["gmail_search", "web_fetch", "web_search"]


def test_ensure_non_empty_respects_k_cap_on_fallback() -> None:
    """The fallback honours ``k`` — it surfaces the best guesses, not everything."""

    tools = select_tools(_registry(), "azerty qwerty zzz", k=2, min_score=1, ensure_non_empty=True)
    assert len(tools) == 2


def test_ensure_non_empty_is_a_noop_when_gate_already_matches() -> None:
    """When the lexical gate already returns tools, the flag changes nothing."""

    gated = select_tools(_registry(), "Trouve le dernier mail reçu", k=8, min_score=1)
    safety = select_tools(
        _registry(), "Trouve le dernier mail reçu", k=8, min_score=1, ensure_non_empty=True
    )
    assert _names(gated) == _names(safety) == ["gmail_search"]


# ---------------------------------------------------------------------------
# Ordering — accent / stop-word normalisation, deterministic tie-break by name.
# ---------------------------------------------------------------------------


def test_ordering_is_descending_score_then_name() -> None:
    """Two web tools tie on a ``web`` token → ordered by ascending name.

    « lire la page web » hits both ``web_fetch`` (page + web) and ``web_search``
    (web), so web_fetch scores higher and sorts first; a same-score tie would
    fall back to ascending name.
    """

    tools = select_tools(_registry(), "lire la page web", k=8, min_score=1)
    names = _names(tools)
    assert names[0] == "web_fetch"  # page + web both hit → higher score
    assert "web_search" in names
    assert "gmail_search" not in names


def test_accent_and_stopwords_normalised() -> None:
    """« la MÉTÉO » matches ``web_search`` despite accents / case / stop-words."""

    tools = select_tools(_registry(), "Quelle est la MÉTÉO ?", k=8, min_score=1)
    assert _names(tools) == ["web_search"]


def test_repeated_tokens_do_not_inflate_score() -> None:
    """Token repetition does not change selection (distinct-token scoring)."""

    once = select_tools(_registry(), "mail", k=8, min_score=1)
    many = select_tools(_registry(), "mail mail mail mail", k=8, min_score=1)
    assert _names(once) == _names(many) == ["gmail_search"]


# ---------------------------------------------------------------------------
# k cap / min_score / no-zero-score-pad / always_on fallback.
# ---------------------------------------------------------------------------


def test_k_cap_truncates_by_score() -> None:
    """``k`` caps the relevance-retrieved tools by descending score.

    « web page mail » hits all three; ``k=1`` keeps only the single highest
    scorer (web_fetch: page + web), dropping the rest.
    """

    tools = select_tools(_registry(), "web page mail", k=1, min_score=1)
    assert _names(tools) == ["web_fetch"]


def test_min_score_threshold_excludes_weak_matches() -> None:
    """A higher ``min_score`` drops tools that only weakly match.

    « courrier » hits ``gmail_search`` only via the low-weight DESCRIPTION-less
    path (no token match here, score 0) — but « mail » hits the tag (weight 3).
    With ``min_score`` above the tag weight, even the mail tag match is dropped.
    """

    # mail → gmail tag (weight 3). min_score=4 is above it → nothing qualifies.
    tools = select_tools(_registry(), "mail", k=8, min_score=4)
    assert tools == []
    # min_score=3 keeps it (>= threshold).
    kept = select_tools(_registry(), "mail", k=8, min_score=3)
    assert _names(kept) == ["gmail_search"]


def test_no_zero_score_padding() -> None:
    """A focused goal is NOT padded up to ``k`` with zero-score tools.

    « mail » matches only gmail_search; even with ``k=8`` and ``min_score=0``
    the two zero-score web tools are NOT added — fewer is desired.
    """

    tools = select_tools(_registry(), "mail", k=8, min_score=0)
    assert _names(tools) == ["gmail_search"]


def test_empty_retrieval_falls_back_to_always_on() -> None:
    """A junk goal with an always_on core returns exactly that core.

    The model is never left tool-less: an always_on tool is advertised even on a
    zero-relevance goal.
    """

    core = _tool("say", description="Parle à l'utilisateur.", always_on=True)
    registry = SubAgentToolRegistry([core, _GMAIL, _WEB_SEARCH])
    tools = select_tools(registry, "azerty qwerty zzz", k=8, min_score=1)
    assert _names(tools) == ["say"]


def test_always_on_listed_first_and_does_not_consume_k() -> None:
    """always_on tools head the list and do NOT count against the ``k`` cap.

    With ``k=1`` and a goal hitting two scored tools, the always_on core plus
    the single top scorer survive — the core is "free".
    """

    core = _tool("say", description="Parle à l'utilisateur.", always_on=True)
    registry = SubAgentToolRegistry([core, _GMAIL, _WEB_SEARCH, _WEB_FETCH])
    tools = select_tools(registry, "web page mail", k=1, min_score=1)
    names = _names(tools)
    assert names[0] == "say"  # always_on first
    assert len(names) == 2  # say (free) + 1 retrieved (k=1)
    assert names[1] == "web_fetch"  # top scorer (page + web)


def test_always_on_not_double_listed_when_also_relevant() -> None:
    """An always_on tool that ALSO scores is listed once (in the always_on slot)."""

    core = _tool("web_search", description="Cherche le web.", tags=("web",), always_on=True)
    registry = SubAgentToolRegistry([core, _GMAIL])
    tools = select_tools(registry, "web mail", k=8, min_score=1)
    names = _names(tools)
    assert names.count("web_search") == 1
    assert names == ["web_search", "gmail_search"]


# ---------------------------------------------------------------------------
# Scaling — a ~30-tool registry advertises ≤ k for a focused goal.
# ---------------------------------------------------------------------------


def test_thirty_tool_registry_caps_focused_goal_at_k() -> None:
    """A focused goal over ~30 tools advertises ≤ ``k`` tools (the whole point).

    The simulated fleet has one tool whose tags match the goal cluster plus 29
    noise tools. With ``k=5`` the advertised set is bounded by ``k`` even though
    the registry is six times larger.
    """

    tools_defs: list[SubAgentToolDefinition] = [
        _tool(
            "calendar_search",
            description="Recherche dans le calendrier (réunion, événement, agenda).",
            tags=("calendrier", "agenda", "réunion", "événement", "calendar"),
        )
    ]
    # 29 unrelated noise tools — distinct names, no overlap with the goal cluster.
    for i in range(29):
        tools_defs.append(
            _tool(f"noise_tool_{i:02d}", description=f"Outil de remplissage numéro {i}.")
        )
    registry = SubAgentToolRegistry(tools_defs)

    k = 5
    tools = select_tools(registry, "Trouve ma prochaine réunion dans l'agenda", k=k, min_score=1)
    assert len(tools) <= k
    # The genuinely relevant tool is among them.
    assert "calendar_search" in _names(tools)


# ---------------------------------------------------------------------------
# The production default registry carries retrieval tags.
# ---------------------------------------------------------------------------


def test_default_registry_tools_carry_retrieval_tags() -> None:
    """Acceptance: the shipped Gmail + web tools carry retrieval ``tags``."""

    registry = build_default_subagent_registry()
    by_name = {d.name: d for d in registry}
    assert by_name["gmail_search"].tags  # non-empty
    assert "mail" in by_name["gmail_search"].tags
    assert by_name["web_search"].tags
    assert "web" in by_name["web_search"].tags
    assert by_name["web_fetch"].tags
    # None of the shipped tools are always_on (no forced core today).
    assert all(not d.always_on for d in registry)


def test_default_registry_mail_goal_excludes_web() -> None:
    """End-to-end over the SHIPPED registry: « dernier mail » excludes web tools."""

    registry = build_default_subagent_registry()
    tools = select_tools(registry, "Trouve le dernier mail de Paul", k=8, min_score=1)
    names = _names(tools)
    assert names == ["gmail_search"]


# ---------------------------------------------------------------------------
# score_tools — the debug scoreboard helper (observability, not selection).
# ---------------------------------------------------------------------------


def test_score_tools_reports_every_tool_descending() -> None:
    """``score_tools`` returns one ``(name, score)`` per tool, score-descending."""

    board = score_tools(_registry(), "Donne-moi l'actu et la météo")
    # Every registered tool is present, exactly once.
    assert {name for name, _ in board} == {"gmail_search", "web_search", "web_fetch"}
    scores = [score for _, score in board]
    assert scores == sorted(scores, reverse=True)
    # web_search owns the matching tags (actu/météo) → it tops the board.
    assert board[0][0] == "web_search"
    assert board[0][1] > 0


def test_score_tools_ties_broken_by_name() -> None:
    """Zero-relevance goal → all scores tie at 0, ordered by ascending name."""

    board = score_tools(_registry(), "azerty qwerty zzz")
    assert board == [("gmail_search", 0), ("web_fetch", 0), ("web_search", 0)]
