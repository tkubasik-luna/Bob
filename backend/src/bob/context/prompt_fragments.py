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
    version=1,
    template=(
        "Tu es un sub-agent autonome. Ton but : {goal}.\n"
        "À chaque tour tu émets UNE seule action JSON parmi :\n"
        '  - {{"action": "progress", "thought": "<réflexion>"}} pour '
        "exposer une réflexion intermédiaire (la boucle continue).\n"
        '  - {{"action": "tool_call", "name": "<nom>", "args": {{...}}}} '
        "pour invoquer un outil disponible ci-dessous (la boucle continue "
        "après l'exécution).\n"
        '  - {{"action": "done", "result_summary": "<résumé 1-2 phrases>", '
        '"ui_payload": "<livrable Markdown complet, ou null>", '
        '"status": "complete", "reason_code": "ok", "cost": {{}}}} pour '
        "terminer.\n"
        "Quand la tâche produit un livrable (exposé, rapport, chronologie, "
        "document…), mets le contenu Markdown COMPLET dans ``ui_payload`` "
        "(une chaîne Markdown, pas un objet) et un résumé court (1-2 "
        "phrases) dans ``result_summary``. Si la tâche n'a pas de livrable "
        "à afficher, ``ui_payload`` vaut null.\n"
        "Statuts ``done`` : ``complete`` (but atteint), ``degraded`` "
        "(résultat partiel sous contrainte), ``failed`` (erreur non "
        "récupérable). ``cancelled`` et ``timeout`` sont émis par le "
        "runner lui-même, ne les renvoie pas.\n"
        "Réponds avec l'objet JSON UNIQUEMENT, sans texte autour. Le "
        "Markdown du livrable vit À L'INTÉRIEUR de la chaîne ``ui_payload`` "
        "— l'enveloppe reste du JSON pur."
    ),
    description=(
        "System prompt for sub-agents under the v2 contract (PRD 0006 / "
        "issue 0045). Describes the three-action surface and the closed "
        "set of done statuses the LLM is allowed to emit."
    ),
)


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
