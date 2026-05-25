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
    version=1,
    template=(
        "\n\nTu disposes de trois outils :\n"
        "- ``spawn_subtask`` : pour déléguer une tâche longue ou autonome à un "
        "sub-agent en arrière-plan.\n"
        "- ``forward_to_subtask`` : pour transmettre la réponse de l'utilisateur "
        "à une sous-tâche en attente d'input. Tu connais l'``id`` de chaque "
        "sous-tâche concernée via le résumé des tâches actives ci-dessous.\n"
        "- ``cancel_subtask`` : pour annuler une sous-tâche en cours quand "
        "l'utilisateur demande explicitement de l'arrêter (\"annule X\", "
        '"laisse tomber").\n'
        "Pour CE message, tu dois EXCLUSIVEMENT :\n"
        "- soit appeler ``spawn_subtask`` (un seul appel) si la demande mérite "
        "d'être déléguée ;\n"
        "- soit appeler ``forward_to_subtask`` si l'utilisateur répond à une "
        "question préalablement transmise par toi pour le compte d'une tâche en "
        "cours ;\n"
        "- soit appeler ``cancel_subtask`` si l'utilisateur demande explicitement "
        "d'annuler / arrêter une tâche listée dans le résumé ;\n"
        "- soit répondre directement en texte si aucune action n'est requise.\n"
        "Ne fais jamais deux appels en parallèle."
    ),
    description=(
        "Appended to the live system prompt for every ``complete()`` call so "
        "Jarvis knows the available Jarvis-side tools."
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
    version=1,
    template=(
        "La sous-tâche '{task_title}' vient de terminer.\n"
        "Résultat brut : '{result}'.\n"
        "Étape 1 — Vérifie le contenu : si le résultat est vide, incohérent ou "
        "manifestement raté, dis-le franchement à l'utilisateur en une phrase "
        "et arrête-toi là.\n"
        "Étape 2 — Sinon, ouvre impérativement par "
        "« Voilà ce que j'ai trouvé à propos de <sujet> … » (remplace <sujet> "
        "par le thème exact de la sous-tâche, pas son titre brut) puis résume "
        "les points clés en 2-3 lignes max dans ton ton. "
        "Propose une suite si pertinent."
    ),
    description=(
        "Prompt fed to Jarvis when a sub-task emits ``done`` and we want "
        "Jarvis to announce + frame the result in his tone."
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
