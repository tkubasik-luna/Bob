"""Versioned prompt fragments — externalised French phrasing templates.

PRD 0006 / issue 0046. Pre-0046 the Jarvis orchestrator carried inline
French phrasing as module-level constants (``_SPAWN_CONFIRMATION``,
``_TOOLS_SYSTEM_ADDENDUM``, ``_DONE_SYNTHESIS_TEMPLATE``…). Issue 0046
moves them here so:

1. The orchestrator stays plumbing-only and does not own user-facing copy.
2. Every fragment is explicitly versioned via :class:`PromptFragment`.
   When we change the wording we bump ``version`` and the snapshot tests
   loudly fail, forcing a conscious review.
3. New providers (system block, summariser, …) can import the same
   fragments rather than re-declare them. One mental model.

The :class:`PromptFragment` dataclass is intentionally tiny — ``id``,
``version``, ``template`` and an optional ``description``. Rendering is a
plain ``str.format`` over the ``template`` with named keyword arguments;
templates with no placeholders are rendered as-is.

Future locales will sit alongside (``personality_v1_fr``,
``personality_v1_en``…) — i18n is out of scope for the PRD but the
``_fr`` / ``_v1`` suffix convention is reserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: French weekday names indexed by ``datetime.weekday()`` (Monday == 0).
_FR_WEEKDAYS = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
#: French month names indexed by ``month - 1``.
_FR_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def temporal_context_fragment(now: datetime | None = None) -> str:
    """Render a one-line French statement of the current date.

    Injected verbatim into the Jarvis and sub-agent system prompts so the
    LLM always knows "today" without guessing (the local models otherwise
    hallucinate a stale year — see the ``gmail_search`` ``after:"2024-…"``
    stall). Both the human-readable form and the ISO date are surfaced: the
    former answers "quel jour on est ?", the latter is the exact token the
    model should pass to date-typed tool arguments.

    ``now`` is injectable for deterministic tests; production passes ``None``
    so the fragment is re-evaluated on every call (a session may span days).
    """

    moment = now or datetime.now()
    weekday = _FR_WEEKDAYS[moment.weekday()]
    month = _FR_MONTHS[moment.month - 1]
    iso = moment.strftime("%Y-%m-%d")
    return (
        f"Contexte temporel : nous sommes le {weekday} {moment.day} {month} "
        f"{moment.year} (date du jour au format ISO : {iso}). Utilise cette "
        "date pour tout raisonnement relatif (« aujourd'hui », « hier », "
        "« ce mois-ci ») et comme valeur des arguments d'outils attendant "
        "une date."
    )


@dataclass(frozen=True)
class PromptFragment:
    """A single, versioned prompt template.

    Fields:

    - ``id`` — stable identifier used by call sites (``"spawn_confirmation"``,
      ``"tools_system_addendum"``…). Never reused for a different copy.
    - ``version`` — integer; bump when the wording changes. The orchestrator
      / tests assert on the version when they need to detect copy drift.
    - ``template`` — Python ``str.format``-compatible template. ``{}``
      placeholders are interpolated by :meth:`render` with named kwargs.
    - ``description`` — short human-readable note explaining when the
      template is used. Optional.
    """

    id: str
    version: int
    template: str
    description: str = ""

    def render(self, **kwargs: object) -> str:
        """Render ``template`` with ``kwargs``; equivalent to ``str.format``."""

        if not kwargs:
            return self.template
        return self.template.format(**kwargs)


# --- Confirmation fragments emitted after dispatching a Jarvis tool call. ---
#
# Each one matches the pre-0046 orchestrator constant. Wording is preserved
# byte-for-byte at v1 — the version field is the lever future changes pull.

SPAWN_CONFIRMATION = PromptFragment(
    id="spawn_confirmation",
    version=1,
    template="D'accord, je m'en occupe. Je te dis dès que c'est prêt.",
    description=(
        "Spoken confirmation when Jarvis successfully spawns one or more "
        "sub-tasks for the user via ``spawn_subtask`` / ``spawn_task``."
    ),
)


FORWARD_CONFIRMATION = PromptFragment(
    id="forward_confirmation",
    version=1,
    template="Compris, je transmets à la tâche.",
    description=(
        "Spoken confirmation when Jarvis forwards the user's reply to a "
        "sub-task waiting for input via ``forward_to_subtask``."
    ),
)


CANCEL_CONFIRMATION = PromptFragment(
    id="cancel_confirmation",
    version=1,
    template="Compris, j'annule.",
    description=(
        "Spoken confirmation when Jarvis cancels a sub-task on the user's "
        "explicit request via ``cancel_subtask``."
    ),
)


# --- System-prompt addendums injected by the orchestrator on each turn. ---

TOOLS_SYSTEM_ADDENDUM = PromptFragment(
    id="tools_system_addendum",
    version=4,
    template=(
        "\n\nTu disposes des outils suivants :\n"
        "- ``say`` : pour répondre directement à l'utilisateur. C'est ton "
        "outil par défaut. ``speech`` (obligatoire) est le texte à dire ; "
        "``ui`` (optionnel) est un objet ``{component, props}`` ou ``null``.\n"
        "- ``show_task_result`` : pour ressortir le livrable d'une tâche "
        "déjà terminée et stockée. Fournis ``speech`` (1 phrase d'intro) "
        "et ``query`` (mots-clés pour retrouver la tâche). Le backend "
        "affiche le Markdown stocké — NE RÉ-GÉNÈRE PAS le contenu.\n"
        "- ``spawn_task`` : pour déléguer une tâche longue ou autonome à "
        "un sub-agent en arrière-plan (version v2 PRD 0006).\n"
        "- ``addendum_task`` : pour ajouter une info à une sous-tâche "
        "déjà en cours sans la redémarrer. Le bloc STATE en tête de "
        "prompt liste l'``id`` exact de chaque tâche active.\n"
        "- ``replan_task`` : pour remplacer une sous-tâche en cours par "
        "une nouvelle version (cancel + respawn avec ``lineage``).\n"
        "- ``cancel_task`` : pour annuler une sous-tâche listée dans le "
        "bloc STATE.\n"
        "RÈGLE ABSOLUE : chaque tour DOIT être exactement UN appel d'outil. "
        "Tu n'écris JAMAIS de texte libre — toute réponse passe par "
        "``say``. Pour CE message :\n"
        "- appelle ``spawn_task`` si la demande mérite d'être déléguée ;\n"
        "- appelle ``addendum_task`` si l'utilisateur enrichit une "
        "tâche active (« ajoute X », « précise Y ») ;\n"
        "- appelle ``replan_task`` si l'utilisateur reformule une "
        "tâche active (« non, plutôt Y ») ;\n"
        "- appelle ``cancel_task`` si l'utilisateur demande "
        "explicitement d'annuler / arrêter une tâche du bloc STATE "
        '("annule X", "laisse tomber") ;\n'
        "- appelle ``show_task_result`` si l'utilisateur veut revoir ou "
        "être ré-informé sur un sujet qu'une sous-tâche a déjà traité "
        "(« ressors X », « rappelle-moi ce que tu avais trouvé sur Y ») ;\n"
        "- sinon, appelle ``say`` avec ton texte de réponse dans ``speech``.\n"
        "Quand tu annonces le résultat d'une tâche terminée, lis la "
        "valeur ``recency`` du bloc STATE : ``active`` → formule du "
        "type « Voilà X… » ; ``stale`` → formule du type « Tu m'avais "
        "demandé X, voilà… ». Ne reprends pas ces patrons "
        "littéralement ; reste naturel.\n"
        "Si un outil renvoie ``scheduler_queue_full``, appelle ``say`` "
        "pour expliquer que tu es à la limite (3 tâches actives, 5 en "
        "file) et demande à l'utilisateur d'en annuler une.\n"
        "Ne fais jamais deux appels en parallèle. Ne renvoie jamais de "
        "texte hors d'un appel d'outil."
    ),
    description=(
        "Appended to the live system prompt for every ``complete()`` call so "
        "Jarvis knows the available Jarvis-side tools. Issue 0050 (v3) "
        "advertises the v2 task surface (``spawn_task`` / "
        "``addendum_task`` / ``replan_task`` / ``cancel_task``) and "
        "instructs the LLM to read the ``recency`` signal from the "
        "STATE block. The legacy v1 ``*_subtask`` tools remain in the "
        "registry as deprecated aliases for the migration."
    ),
)


# --- Proactivity templates used by the post-turn renderers. ---
#
# These were pinned in code pre-0046 (no jarvis.md tuning) and remain so —
# version 1 carries the same wording byte-for-byte.

ASK_USER_PARAPHRASE_TEMPLATE = PromptFragment(
    id="ask_user_paraphrase",
    version=1,
    template=(
        "Une de tes sous-tâches ({task_title}) a besoin d'une info : "
        "'{raw_question}'. Reformule cette question pour l'utilisateur dans "
        "ton ton, en 1-2 phrases max. Ne mentionne pas le mot 'sub-agent', "
        "dis 'la tâche'."
    ),
    description=(
        "Prompt fed to Jarvis when a sub-task emits ``ask_user`` and we want "
        "Jarvis to paraphrase the raw question in his tone."
    ),
)


DONE_SYNTHESIS_TEMPLATE = PromptFragment(
    id="done_synthesis",
    version=2,
    template=(
        "La sous-tâche '{task_title}' vient de terminer.\n"
        "Résultat brut : '{result}'.\n"
        "Ta réponse sera LUE À VOIX HAUTE (TTS) — elle doit donc être très "
        "courte et parlée.\n"
        "Étape 1 — Vérifie le contenu : si le résultat est vide, incohérent ou "
        "manifestement raté, dis-le franchement à l'utilisateur en une phrase "
        "et arrête-toi là.\n"
        "Étape 2 — Sinon, ouvre par « Voilà ce que j'ai trouvé à propos de "
        "<sujet> … » (remplace <sujet> par le thème exact de la sous-tâche, "
        "pas son titre brut), puis donne UNIQUEMENT l'essentiel en 2 phrases "
        "courtes maximum (~40 mots au total). Interdits : listes, titres, "
        "énumérations, markdown, et tout détail du résultat brut au-delà de ces "
        "2 phrases — l'utilisateur ouvrira le résultat complet s'il veut le "
        "détail. Termine par une seule question de relance courte."
    ),
    description=(
        "Prompt fed to Jarvis when a sub-task emits ``done`` and we want "
        "Jarvis to announce + frame the result in his tone."
    ),
)


FAILED_SYNTHESIS_TEMPLATE = PromptFragment(
    id="failed_synthesis",
    version=1,
    template=(
        "La sous-tâche '{task_title}' a échoué.\n"
        "Raison brute : '{result}'.\n"
        "Annonce l'échec à l'utilisateur en 1-2 phrases max dans ton ton, "
        "sans jargon technique (ne dis pas 'sub-agent', dis 'la tâche'). "
        "Si la raison est parlante (ex : trop long, délai dépassé / timeout), "
        "explique-la simplement, puis propose de réessayer ou de découper la "
        "demande en plus petit. Ne fais pas semblant d'avoir un résultat."
    ),
    description=(
        "Prompt fed to Jarvis when a sub-task transitions to ``failed`` "
        "(natural failure — not a user cancel) so Jarvis announces the "
        "failure + a recovery suggestion in his tone."
    ),
)


# --- Summariser fragments (issue 0046 RollingSummaryProvider). ---

SUMMARISER_SYSTEM_PROMPT = PromptFragment(
    id="summariser_system",
    version=1,
    template=(
        "Tu es un agent de résumé. Tu reçois une liste de tours de "
        "conversation entre Tom (l'utilisateur) et Jarvis (l'assistant) et "
        "tu produis un résumé concis (3-6 lignes max) en français. "
        "Garde uniquement les informations factuelles persistantes : sujets "
        "abordés, décisions prises, tâches déléguées, préférences exprimées. "
        "N'invente rien, ne paraphrase pas les questions de Jarvis."
    ),
    description=(
        "System prompt for the LLM-backed summariser. Always run against RAW "
        "older turns — never against the prior digest, to bound drift."
    ),
)


SUMMARISER_USER_PROMPT = PromptFragment(
    id="summariser_user",
    version=1,
    template=(
        "Voici les tours plus anciens à résumer "
        "(du tour {from_turn} au tour {to_turn}) :\n\n{transcript}\n\n"
        "Résume-les en 3-6 lignes maximum."
    ),
    description=("Templated transcript wrapper handed to the LLM-backed summariser."),
)


SUMMARY_BLOCK_HEADER = PromptFragment(
    id="summary_block_header",
    version=1,
    template=(
        "Résumé des échanges plus anciens "
        "(tours {from_turn} à {to_turn}, version {summariser_version}) :\n"
        "{summary}"
    ),
    description=(
        "Wrapper rendered around the persisted rolling summary when it is "
        "injected into the bounded prompt by ``RollingSummaryProvider``."
    ),
)


# --- System-block fragment (bounded policy, no waiting-input block here). ---

SYSTEM_BLOCK_PERSONALITY_REMINDER = PromptFragment(
    id="system_block_personality_reminder",
    version=1,
    template=(
        "\n\nReste concis et naturel. Ne ré-explique pas le contexte à chaque "
        "tour ; le résumé ci-dessus contient déjà l'historique pertinent."
    ),
    description=(
        "Tail added to the system prompt under the bounded policy so the "
        "model is reminded the rolling summary already carries older context."
    ),
)


# --- Sub-agent v2 fragments (PRD 0006 / issue 0045). ---
#
# The sub-agent v2 contract surfaces three structured actions. The system
# prompt below describes the action surface to the LLM so the runner can
# parse a versioned :class:`bob.sub_agent.actions.SubAgentAction`. Tools
# are listed dynamically by the runner — keep the prompt fragment focused
# on the action contract itself.

SUB_AGENT_V2_SYSTEM_PROMPT = PromptFragment(
    id="sub_agent_v2_system",
    version=7,
    template=(
        "You are an autonomous sub-agent. Your goal: {goal}.\n"
        "Reason internally in English. ALL user-facing text you write — "
        "``result_summary`` and the Markdown inside ``ui_payload`` — MUST be "
        "in French (the user, Tom, reads French).\n"
        "At each turn you emit EXACTLY ONE JSON action among:\n"
        '  - {{"action": "progress", "thought": "<reasoning>"}} to expose '
        "intermediate reasoning (the loop continues).\n"
        '  - {{"action": "tool_call", "name": "<name>", "args": {{...}}}} '
        "to invoke an available tool listed below (the loop continues after "
        "execution).\n"
        '  - {{"action": "done", "result_summary": "<1-2 sentence summary, '
        'in French>", "ui_payload": "<full Markdown deliverable in French, or '
        'null>", "result_ref": "<ref to a tool result, or null>", '
        '"status": "complete", "reason_code": "ok", "cost": {{}}}} to '
        "finish.\n"
        "When the task produces a deliverable that YOU author (briefing, "
        "report, timeline, document…), put the COMPLETE Markdown content (in "
        "French) in ``ui_payload`` (a Markdown string, not an object) and a "
        "short summary (1-2 sentences, in French) in ``result_summary``. If "
        "the task has no deliverable to display, ``ui_payload`` is null.\n"
        "When a tool has returned a result, its ``tool`` message contains a "
        "short identifier ``result_ref`` (e.g. ``tool#1``). To conclude FROM "
        "that result, put this ``result_ref`` in ``done``: the displayable "
        "deliverable (card, preview…) is rebuilt AUTOMATICALLY server-side — "
        "you do NOT need to copy the result data into ``ui_payload``.\n"
        "``done`` statuses: ``complete`` (goal reached), ``degraded`` "
        "(partial result under constraint), ``failed`` (non-recoverable "
        "error). ``cancelled`` and ``timeout`` are emitted by the runner "
        "itself — do not return them.\n"
        "\n"
        "Reply with the JSON object ONLY, no surrounding text. The "
        "deliverable Markdown lives INSIDE the ``ui_payload`` string — the "
        "envelope stays pure JSON."
    ),
    description=(
        "Base system prompt for sub-agents under the v2 contract (PRD 0006 / "
        "issue 0045). Describes the three-action surface and the closed set "
        "of done statuses the LLM is allowed to emit — and nothing tool-"
        "specific. Issue 0063 (v5) extracted the Gmail email-lookup recipe "
        "into :data:`GMAIL_SEARCH_SKILL_PACK`. PRD 0009 (v6) adds the "
        "``result_ref`` finishing path: a tool result carries a short ref, and "
        "``done`` may reference it instead of copying the data into "
        "``ui_payload`` (the server rebuilds the deliverable from the stored "
        "result deterministically). The example ref is deliberately generic "
        "(``tool#1``) so the base contract names no specific tool. v7 switches "
        "the contract + internal reasoning to English (better CoT / tool-call "
        "accuracy on small local models) while pinning all user-facing output "
        "(``result_summary``, ``ui_payload``) to French."
    ),
)


# --- Sub-agent skill packs (PRD 0008 / issue 0063). ---
#
# Tools are typed functions (act); skills are instruction packs (workflow).
# A :class:`SkillPack` is a goal-triggered block appended to the sub-agent
# system prompt only when its trigger keywords match the task goal, so the
# base action contract above stays focused while task-specific recipes live
# beside it. The Gmail recipe below was inline in ``SUB_AGENT_V2_SYSTEM_PROMPT``
# through v4 (issues 0055 / 0056); v5 relocated it here verbatim.


@dataclass(frozen=True)
class SkillPack:
    """A goal-triggered instruction pack appended to the sub-agent prompt.

    Fields:

    - ``id`` / ``version`` — stable identity + drift lever, mirroring
      :class:`PromptFragment`.
    - ``fragment`` — the :class:`PromptFragment` carrying the recipe copy. It
      is rendered with no kwargs (the recipe is literal instructional text, so
      any ``{...}`` it contains is a single-brace placeholder the *model*
      fills, not a Python ``str.format`` slot).
    - ``triggers`` — lowercased substrings; the pack loads when ANY appears in
      the (lowercased) goal. Substring (not word) matching is deliberate so
      ``mail`` also fires on ``email`` / ``gmail`` / ``e-mail``.
    """

    id: str
    version: int
    fragment: PromptFragment
    triggers: tuple[str, ...]

    def matches(self, goal: str) -> bool:
        """True when any trigger keyword appears in ``goal`` (case-insensitive)."""

        lowered = goal.lower()
        return any(trigger in lowered for trigger in self.triggers)

    def render(self) -> str:
        """Render the skill's instructional copy (no interpolation)."""

        return self.fragment.render()


GMAIL_SEARCH_SKILL_PACK = SkillPack(
    id="gmail_search_recipe",
    version=3,
    fragment=PromptFragment(
        id="gmail_search_recipe",
        version=3,
        template=(
            "Special case — mail lookup. When the goal is to find an email in "
            "the user's Gmail inbox:\n"
            '  1. (optional) ``progress(thought="searching Gmail")`` to signal '
            "you are starting.\n"
            "  2. Call ``gmail_search`` with the most specific filters you can "
            "infer from the goal (``from_name``, ``from_email``, "
            "``subject_contains``, ``after``, ``before``, ``has_attachment``, "
            "``label``). ``max_results`` stays at 1 unless the goal explicitly "
            "mentions several mails. NEVER call ``gmail_search`` without at "
            "least one filter — an argument-less call is rejected. **Mandatory "
            "fallback for a generic goal** (« dernier mail », « dernier mail "
            "reçu », « ma boîte », « inbox », with no sender, subject or "
            'date): ``label="INBOX"``. For « dernier mail envoyé » with no '
            'target: ``label="SENT"``. NEVER retry the same filter-less call '
            "— pick the ``label`` fallback on the first attempt if nothing "
            "else is inferable.\n"
            "  3. That is all for the nominal case: as soon as ``gmail_search`` "
            "returns a result (empty or not), the Mail card and the spoken "
            "summary are built AUTOMATICALLY and the task ends. You neither "
            "write the ``done`` nor copy the mail ``props``. (If you ever "
            "regain control after the result, finish with ``done`` carrying "
            "the result's ``result_ref`` — no ``ui_payload`` needed.)\n"
            "\n"
            "If ``gmail_search`` returns an ERROR (not a result), it is up to "
            'YOU to conclude: emit ``done(status="failed", ui_payload=null)`` '
            "whose ``result_summary`` will be read aloud, verbatim. The spoken "
            "summary MUST be in French, using these exact sentences:\n"
            "  - ``gmail_search_bootstrap_required`` / "
            "``gmail_search_refresh_failed`` / ``gmail_search_auth_failed`` "
            "(OAuth access expired/revoked): « Mon accès à Gmail a expiré — "
            "relance le script de connexion (python -m "
            "bob.connectors.gmail.auth). » — the path ``python -m "
            "bob.connectors.gmail.auth`` MUST appear literally.\n"
            "  - ``gmail_search_api_unreachable`` (Gmail 5xx / quota / network "
            "timeout): « Je n'ai pas pu joindre Gmail à l'instant — réessaie "
            "dans un moment. »\n"
            "  - ``gmail_search_invalid_query`` / ``gmail_search_failed``: "
            "« Je n'ai pas pu effectuer la recherche Gmail — vérifie ta "
            "demande. »\n"
            "  - ``invalid_args`` (tool validation): retry ``gmail_search`` "
            "WITH a filter this time; if the error persists, "
            '``done(status="failed", ui_payload=null, '
            "result_summary=\"Je n'ai pas su construire la recherche "
            'Gmail.")``.\n'
            "Stick to these French sentences word for word (substitute the "
            "searched name/email if relevant)."
        ),
        description=(
            "Gmail email-lookup recipe — slimmed in PRD 0009 (v2). The happy "
            "path no longer asks the model to hand-build the "
            "``{component:'Mail', props}`` descriptor or handle the empty "
            "result: a successful gmail_search is a *terminal* projection, so "
            "the runner converges and builds the Mail card + spoken summary "
            "deterministically from the stored result. The pack now covers "
            "only (a) building a good search (filters + INBOX/SENT fallback "
            "for generic goals) and (b) the tool-ERROR branches, which do not "
            "converge and still need a model-authored ``done(failed)`` with a "
            "pinned French speech. v3 (matching base prompt v7) switches the "
            "recipe instructions to English for small-model reliability while "
            "keeping the verbatim spoken error sentences in French (they are "
            "read aloud to the user via TTS)."
        ),
    ),
    triggers=("mail", "gmail", "courriel", "messagerie", "boîte", "inbox"),
)


WEB_SEARCH_SKILL_PACK = SkillPack(
    id="web_search_recipe",
    version=1,
    fragment=PromptFragment(
        id="web_search_recipe",
        version=1,
        template=(
            "Special case — web research. When the goal needs information from "
            "the internet (facts, current events, prices, definitions — anything "
            "not in the user's mailbox):\n"
            '  1. (optional) ``progress(thought="recherche web")``.\n'
            "  2. Call ``web_search`` with a FOCUSED query: rephrase the goal "
            "into good search keywords, do NOT paste the whole sentence. Leave "
            "``max_results`` unset (server default) unless the goal needs "
            "breadth. NEVER call ``web_fetch`` before ``web_search``.\n"
            "  3. Read the results. Two outcomes:\n"
            "     a. The answer is already clear from the snippets (or the "
            "result's ``answer``). Conclude WITHOUT fetching: "
            '``done(status="done", result_ref="web_search#1", '
            'result_summary="<one-line French answer>")``. The sources card is '
            "rebuilt AUTOMATICALLY from ``result_ref`` — do NOT hand-build "
            "``ui_payload``.\n"
            "     b. One result must be read in full (long article, deep "
            "detail). Call ``web_fetch`` on the SINGLE most relevant ``url``, "
            "read the returned text, then synthesise: "
            '``done(status="done", ui_payload={"component":"Markdown",'
            '"props":{"content":"<written answer in French, citing sources as '
            '[titre](url)>"}}, result_summary="<short spoken French summary>")``.'
            "\n"
            "  NEVER fetch more than 2 urls. Quote facts from the results — do "
            "NOT invent or rely on memory for anything the search can confirm.\n"
            "\n"
            "If a web tool returns an ERROR (not a result), conclude with "
            '``done(status="failed", ui_payload=null)`` whose ``result_summary`` '
            "is read aloud VERBATIM. Use these exact French sentences:\n"
            "  - ``web_search_missing_key`` / ``web_fetch_missing_key`` : "
            "« La recherche web n'est pas configurée — ajoute une clé Tavily "
            "(TAVILY_API_KEY) dans le fichier .env. » — ``TAVILY_API_KEY`` MUST "
            "appear literally.\n"
            "  - ``web_search_unauthorized`` / ``web_fetch_unauthorized`` : "
            "« Ma clé de recherche web a été refusée — vérifie la clé Tavily. »\n"
            "  - ``web_search_rate_limited`` / ``web_fetch_rate_limited`` : "
            "« J'ai atteint la limite de recherches web — réessaie dans un "
            "moment. »\n"
            "  - ``web_search_api_unreachable`` / ``web_fetch_api_unreachable`` "
            ": « Je n'ai pas pu joindre le service de recherche — réessaie dans "
            "un moment. »\n"
            "  - ``web_search_failed`` / ``web_fetch_failed`` : « Je n'ai pas pu "
            "effectuer la recherche web. »\n"
            "Stick to these French sentences word for word."
        ),
        description=(
            "Web-research recipe for the Tavily-backed ``web_search`` / "
            "``web_fetch`` tools. Covers (a) building a focused query, (b) the "
            "two happy paths — converge on the search via ``result_ref`` (the "
            "WebResults card is rebuilt deterministically) for a snippet-"
            "answerable goal, or ``web_fetch`` + Markdown synthesis for a "
            "read-in-full goal — and (c) the five tool-ERROR branches with "
            "pinned French speech read aloud via TTS. Mirrors the gmail recipe's "
            "shape (English instructions, verbatim French error sentences)."
        ),
    ),
    triggers=(
        "web",
        "internet",
        "en ligne",
        "google",
        "actualité",
        "actualités",
        "news",
        "wikipedia",
        "wikipédia",
        "sur le net",
    ),
)


WEATHER_SKILL_PACK = SkillPack(
    id="weather_recipe",
    version=1,
    fragment=PromptFragment(
        id="weather_recipe",
        version=1,
        template=(
            "Special case — weather lookup. When the goal asks about the weather "
            "(météo, temps, prévision, forecast) for a place and/or a date:\n"
            '  1. (optional) ``progress(thought="recherche météo")``.\n'
            "  2. Extract the PLACE (a city / region) and the DATE from the goal. "
            "If the goal says « demain », « ce week-end », « lundi prochain », "
            "translate it to the concrete date relative to today's date given "
            "above. If no place is stated, ask for it via "
            '``done(status="failed", ui_payload=null, '
            'result_summary="Pour quelle ville veux-tu la météo ?")`` rather '
            "than guessing.\n"
            "  3. Call the forecast tool advertised below with the place "
            "(and date when given). It is a SINGLE-SHOT lookup: as soon as it "
            "returns a result the weather card and the spoken summary are built "
            "AUTOMATICALLY and the task ends — you neither write ``done`` nor "
            "copy the result. (If you ever regain control after the result, "
            "finish with ``done`` carrying the result's ``result_ref`` and a "
            "ONE-LINE French forecast as ``result_summary`` — e.g. « À Paris "
            "demain : ensoleillé, 22 °C. » — no ``ui_payload`` needed.)\n"
            "  NEVER invent a forecast from memory — only report what the tool "
            "returned.\n"
            "\n"
            "If the forecast tool returns an ERROR (not a result), conclude with "
            '``done(status="failed", ui_payload=null)`` whose ``result_summary`` '
            "is read aloud VERBATIM. Use this exact French sentence for any "
            "tool error (``mcp_unreachable`` / ``mcp_missing_server`` / "
            "``mcp_tool_error`` / ``mcp_tool_failed`` / ``invalid_args``):\n"
            "  « Le service météo est indisponible pour le moment — réessaie "
            "dans un instant. »\n"
            "Stick to this French sentence word for word."
        ),
        description=(
            "Weather-lookup recipe for the manifest-driven, terminal MCP "
            "forecast tool (issue 0095). Mirrors the gmail/web packs' shape: "
            "English instructions, a verbatim French error sentence read aloud "
            "via TTS. Covers (a) extracting the place + date from the goal "
            "(resolving relative dates against today's date), (b) the happy "
            "path — a single-shot forecast call CONVERGES deterministically "
            "(the weather card + spoken summary are built from the stored "
            "terminal projection; the model rarely needs to emit ``done``), and "
            "(c) the single tool-ERROR branch with a pinned « service météo "
            "indisponible » French speech. The tool NAME is intentionally left "
            "to the advertised catalogue — the recipe never hard-codes it, so a "
            "manifest rename does not desync the prompt."
        ),
    ),
    triggers=("météo", "meteo", "temps", "weather", "prévision", "prevision"),
)


#: Ordered registry of sub-agent skill packs. :func:`select_skill_packs`
#: filters it by goal; the runner appends the survivors to the system prompt.
SUB_AGENT_SKILL_PACKS: tuple[SkillPack, ...] = (
    GMAIL_SEARCH_SKILL_PACK,
    WEB_SEARCH_SKILL_PACK,
    WEATHER_SKILL_PACK,
)


def select_skill_packs(goal: str) -> list[SkillPack]:
    """Return the skill packs whose triggers match ``goal``, in registry order.

    Issue 0063. The runner appends each survivor to the base sub-agent system
    prompt, so a non-matching goal (e.g. a generic research task) never pays
    for the Gmail recipe's ~60 lines of tokens.
    """

    return [pack for pack in SUB_AGENT_SKILL_PACKS if pack.matches(goal)]


SUB_AGENT_V2_ADDENDUM_TEMPLATE = PromptFragment(
    id="sub_agent_v2_addendum",
    version=1,
    template=(
        "L'utilisateur a ajouté la note suivante en cours de route "
        "(prise en compte pour la suite de la tâche) : « {text} »"
    ),
    description=(
        "Per-addendum wrapper injected into the next sub-agent LLM "
        "iteration when :class:`AddendumQueue.drain` returns entries. "
        "0050 (addendum_task tool) is the producer side."
    ),
)
