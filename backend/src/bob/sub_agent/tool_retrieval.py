"""Goal-driven tool retrieval — PRD 0015 / issue 0092.

The runner used to render the WHOLE sub-agent registry into the prompt on
every turn. With a handful of tools that is fine; once an MCP fleet lands it
drowns a weak local model in irrelevant tool schemas. This module is the
*advertisement* gate: given a registry + a task goal it returns the subset of
tools worth showing the model, while DISPATCH stays on the full registry by
name (the advertised set is a SUBSET of the dispatchable set — a
registered-but-not-advertised tool still resolves when the model calls it; see
:mod:`bob.sub_agent.runner`).

:func:`select_tools` is **pure and deterministic** — no I/O, no model, zero new
dependency. V1 is a hand-rolled, field-weighted lexical keyword score:

- each goal / tool token is accent-stripped, lower-cased, and French stop-words
  are dropped, so « le dernier mail reçu » reduces to ``{dernier, mail, recu}``;
- a tool's score is the weighted count of goal tokens that hit its ``name``
  (highest weight), ``tags`` (mid), or ``description`` (lowest);
- the advertised set is the UNION of the ``always_on`` tools and every tool
  scoring ``>= min_score``, capped at ``k`` by score, tie-broken by name —
  NEVER padded to ``k`` with zero-score tools (fewer is the whole point);
- when nothing scores at or above the threshold the model still gets the
  ``always_on`` core (it is never left tool-less).

Ordering contract: ``always_on`` tools first (so the core is always visible up
top), then the scored survivors by descending score, ties broken by ascending
name. ``always_on`` tools never count against the ``k`` cap — the cap bounds the
*relevance-retrieved* surface, not the guaranteed core.
"""

from __future__ import annotations

import re
import unicodedata

from bob.sub_agent.tool_registry import SubAgentToolDefinition

#: Field weights for the lexical score. Name is the strongest signal (a goal
#: token that hits the tool's own name is a near-certain intent match), tags
#: next (curated retrieval keywords), description lowest (prose, noisier). The
#: ratios — not the absolute values — are what matter to the ranking.
_WEIGHT_NAME = 6
_WEIGHT_TAGS = 3
_WEIGHT_DESCRIPTION = 1

#: French (+ a few English) stop-words stripped from BOTH the goal and each
#: tool's text before scoring, so high-frequency glue words ("le", "de", "et",
#: "sur", "the", …) never create spurious matches. Accent-stripped + lower-cased
#: to match the token normalisation. Deliberately small and hand-curated — this
#: is a keyword scorer, not an NLP pipeline.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        # articles / determiners
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "de",
        "d",
        "l",
        "ce",
        "cet",
        "cette",
        "ces",
        "mon",
        "ma",
        "mes",
        "ton",
        "ta",
        "tes",
        "son",
        "sa",
        "ses",
        "notre",
        "nos",
        "votre",
        "vos",
        "leur",
        "leurs",
        # prepositions / conjunctions
        "et",
        "ou",
        "a",
        "au",
        "aux",
        "en",
        "dans",
        "sur",
        "sous",
        "pour",
        "par",
        "avec",
        "sans",
        "que",
        "qui",
        "quoi",
        "dont",
        "ne",
        "pas",
        "plus",
        "moins",
        "y",
        "se",
        "si",
        "ni",
        "car",
        "donc",
        "or",
        "mais",
        # pronouns / fillers
        "je",
        "tu",
        "il",
        "elle",
        "on",
        "nous",
        "vous",
        "ils",
        "elles",
        "me",
        "te",
        "moi",
        "toi",
        "lui",
        "eux",
        "est",
        "es",
        "suis",
        "etre",
        "ai",
        "as",
        "avoir",
        # English glue ("a" / "on" / "or" / "me" already covered above)
        "the",
        "an",
        "of",
        "to",
        "in",
        "for",
        "and",
        "is",
        "are",
        "my",
        "i",
    }
)

#: Token splitter — runs of word characters (Unicode-aware). Punctuation /
#: whitespace are separators; accents are stripped afterwards in
#: :func:`_normalise_token` so the regex stays simple.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _strip_accents(text: str) -> str:
    """Return ``text`` with combining accents removed (NFKD fold).

    « rené » → ``rene``, « météo » → ``meteo``. Keeps the scorer robust to the
    user typing (or the model omitting) accents.
    """

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _singularize(token: str) -> str:
    """Strip a single trailing French plural marker (``s`` / ``x``).

    Conservative light stemming so « mails » matches the tag ``mail`` and
    « réunions » matches ``réunion``. Applied to BOTH goal and tool tokens so
    the folding is symmetric (``mails`` and ``mail`` collapse to the same key).
    Only fires for tokens longer than three characters so short words
    (``os``, ``as``) are left intact; nouns of three letters or fewer almost
    never carry a meaningful plural distinction for this keyword scorer.
    """

    if len(token) > 3 and token[-1] in ("s", "x"):
        return token[:-1]
    return token


def _normalise_token(token: str) -> str:
    """Lower-case, accent-strip, then light-singularize a single token."""

    return _singularize(_strip_accents(token.lower()))


def _tokenize(text: str) -> list[str]:
    """Split ``text`` into normalised, stop-word-free tokens.

    Order is preserved (the caller usually folds to a set / Counter, but a
    stable list keeps the function easy to reason about and test).
    """

    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        norm = _normalise_token(raw)
        if not norm or norm in _STOP_WORDS:
            continue
        tokens.append(norm)
    return tokens


def _score_tool(definition: SubAgentToolDefinition, goal_tokens: frozenset[str]) -> int:
    """Field-weighted lexical score of ``definition`` against ``goal_tokens``.

    Each DISTINCT goal token contributes the weight of the HIGHEST-weighted
    field it appears in (name > tags > description). Using distinct tokens (a
    set) and best-field-wins keeps the score stable under repetition — a goal
    that says "mail mail mail" does not out-score a focused one, and a token
    living in both the name and the description counts once at the name weight.
    """

    name_tokens = set(_tokenize(definition.name))
    tag_tokens: set[str] = set()
    for tag in definition.tags:
        tag_tokens.update(_tokenize(tag))
    description_tokens = set(_tokenize(definition.description))

    score = 0
    for token in goal_tokens:
        if token in name_tokens:
            score += _WEIGHT_NAME
        elif token in tag_tokens:
            score += _WEIGHT_TAGS
        elif token in description_tokens:
            score += _WEIGHT_DESCRIPTION
    return score


def select_tools(
    registry: object,
    goal: str,
    *,
    k: int,
    min_score: int,
) -> list[SubAgentToolDefinition]:
    """Return the tools worth advertising for ``goal`` — pure + deterministic.

    Selection rule (issue 0092):

    - score every tool lexically (:func:`_score_tool`);
    - keep tools scoring ``>= min_score`` AND every ``always_on`` tool;
    - cap the *relevance-retrieved* tools at ``k`` by descending score (ties by
      ascending name), NEVER padding up to ``k`` with zero-score tools;
    - ``always_on`` tools are always kept and do NOT consume the ``k`` budget;
    - when no tool clears the threshold the result is exactly the ``always_on``
      core (the model is never left tool-less).

    Ordering: ``always_on`` tools first (registry-name order), then the scored
    survivors by descending score, ties broken by ascending name. ``registry``
    is anything iterable over :class:`SubAgentToolDefinition` (the
    :class:`bob.sub_agent.tool_registry.SubAgentToolRegistry` qualifies); typed
    loosely so this module never imports the registry's concrete shape for a
    pure helper. ``k`` / ``min_score`` come from config knobs
    (:attr:`Settings.TOOL_RETRIEVAL_K` / ``TOOL_RETRIEVAL_MIN_SCORE``).
    """

    definitions: list[SubAgentToolDefinition] = list(registry)  # type: ignore[call-overload]
    goal_tokens = frozenset(_tokenize(goal))

    always_on = [d for d in definitions if d.always_on]
    always_on_names = {d.name for d in always_on}

    # Score the non-always-on tools; a tool that is BOTH always_on and relevant
    # is already guaranteed a slot, so it is excluded from the scored pool to
    # avoid double-listing and to keep the k budget for genuinely-retrieved
    # tools.
    scored: list[tuple[int, SubAgentToolDefinition]] = []
    for definition in definitions:
        if definition.name in always_on_names:
            continue
        score = _score_tool(definition, goal_tokens)
        if score >= min_score and score > 0:
            scored.append((score, definition))

    # Descending score, then ascending name — fully deterministic tie-break.
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    capped = scored[: max(k, 0)]

    return [*always_on, *(definition for _score, definition in capped)]


def score_tools(registry: object, goal: str) -> list[tuple[str, int]]:
    """Return ``(tool_name, score)`` for every tool, descending — debug only.

    Exposes the full lexical scoreboard :func:`select_tools` computes internally
    so callers (the runner's debug log) can show WHY a tool was advertised or
    dropped for a goal. ``always_on`` tools are reported with their genuine
    lexical score even though selection keeps them regardless. Pure, no I/O;
    ties broken by ascending name, mirroring :func:`select_tools`.
    """

    goal_tokens = frozenset(_tokenize(goal))
    scored = [
        (definition.name, _score_tool(definition, goal_tokens))
        for definition in registry  # type: ignore[attr-defined]
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored
