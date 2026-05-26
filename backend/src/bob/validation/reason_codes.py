"""Versioned :class:`ReasonCodeRegistry` shared with the frontend (PRD 0006 / issue 0048).

A small, append-only registry of short machine-readable codes the sub-agent
and Jarvis emit when a turn ends in something other than a clean
``done(complete, ok)``. Every code carries:

- ``code`` — the literal string flowed through ``done.reason_code`` and the
  ``DispatchResult.error_code`` field;
- ``actor`` — ``"jarvis"``, ``"sub_agent"``, or ``"shared"`` (used by both);
- ``description`` — short human-readable note that doubles as a translator's
  prompt when the frontend i18n table lists the code;
- ``severity`` — ``"info"`` / ``"warn"`` / ``"error"``.

The registry exposes :attr:`schema_version` so a future LLM-model swap can
detect drift; the frontend mirrors the table via a generated
``frontend/src/generated/reason_codes.ts`` file (see
:func:`bob.validation.reason_codes.write_frontend_table` which runs in a
test to keep the file fresh).

The runtime constants previously living in :mod:`bob.sub_agent.runner` are
re-exported from here so the runner and the dispatcher share one identifier
namespace.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Schema version. Bump whenever a code is renamed (NEVER reuse a retired
#: code for a different meaning — append-only registry).
REASON_CODE_SCHEMA_VERSION = 1


ReasonCodeActor = Literal["jarvis", "sub_agent", "shared"]
ReasonCodeSeverity = Literal["info", "warn", "error"]


@dataclass(frozen=True)
class ReasonCode:
    """One entry in the :class:`ReasonCodeRegistry`."""

    code: str
    actor: ReasonCodeActor
    severity: ReasonCodeSeverity
    description: str


# --- Shared reason codes -----------------------------------------------------
#
# Codes used by both Jarvis (tool-dispatch error path) and the sub-agent
# runner (terminal ``done.reason_code``). Pinned strings — never change a
# value once shipped (frontend i18n keys depend on them).

REASON_OK = "ok"
REASON_ITERATION_CAP = "iteration_cap"
REASON_WALL_CLOCK_CAP = "wall_clock_cap"
REASON_TOKEN_CAP = "token_cap"
REASON_USER_CANCELLED = "user_cancelled"
REASON_HARD_KILLED = "hard_killed"
REASON_INVALID_OUTPUT = "invalid_output"
REASON_LLM_FAILED = "llm_failed"
REASON_TOOL_FAILED = "tool_failed"

# --- Jarvis-side codes -------------------------------------------------------

REASON_VALIDATION_EXHAUSTED = "validation_exhausted"
REASON_UNKNOWN_TASK = "unknown_task"


_REGISTRY: tuple[ReasonCode, ...] = (
    ReasonCode(
        code=REASON_OK,
        actor="shared",
        severity="info",
        description="Tour terminé sans incident.",
    ),
    ReasonCode(
        code=REASON_ITERATION_CAP,
        actor="sub_agent",
        severity="warn",
        description="La sous-tâche a atteint la limite d'itérations.",
    ),
    ReasonCode(
        code=REASON_WALL_CLOCK_CAP,
        actor="sub_agent",
        severity="warn",
        description="La sous-tâche a atteint la limite temporelle.",
    ),
    ReasonCode(
        code=REASON_TOKEN_CAP,
        actor="sub_agent",
        severity="warn",
        description="La sous-tâche a atteint le budget de tokens.",
    ),
    ReasonCode(
        code=REASON_USER_CANCELLED,
        actor="sub_agent",
        severity="info",
        description="La sous-tâche a été annulée par l'utilisateur.",
    ),
    ReasonCode(
        code=REASON_HARD_KILLED,
        actor="sub_agent",
        severity="warn",
        description="La sous-tâche a été tuée après expiration du délai d'annulation.",
    ),
    ReasonCode(
        code=REASON_INVALID_OUTPUT,
        actor="shared",
        severity="warn",
        description="Sortie LLM invalide après épuisement du budget de retry.",
    ),
    ReasonCode(
        code=REASON_LLM_FAILED,
        actor="sub_agent",
        severity="error",
        description="L'appel LLM a échoué de manière irrécupérable.",
    ),
    ReasonCode(
        code=REASON_TOOL_FAILED,
        actor="shared",
        severity="warn",
        description="L'appel d'outil a échoué côté sub-agent.",
    ),
    ReasonCode(
        code=REASON_VALIDATION_EXHAUSTED,
        actor="jarvis",
        severity="warn",
        description="Le validateur a refusé les sorties de Jarvis après tous les retries.",
    ),
    ReasonCode(
        code=REASON_UNKNOWN_TASK,
        actor="jarvis",
        severity="warn",
        description="Jarvis a référencé un ``task_id`` inconnu.",
    ),
)


class ReasonCodeRegistry:
    """Lookup of :class:`ReasonCode` entries by code.

    Append-only: codes are never removed once registered (renames are a
    new entry plus a fresh ``REASON_CODE_SCHEMA_VERSION`` bump). Look up
    is by string id; iteration yields entries in registration order, which
    is the order used to build the frontend table so the frontend tests
    can rely on a stable layout.
    """

    def __init__(self, entries: tuple[ReasonCode, ...] = _REGISTRY) -> None:
        self._entries: tuple[ReasonCode, ...] = entries
        self._by_code: dict[str, ReasonCode] = {entry.code: entry for entry in entries}

    @property
    def schema_version(self) -> int:
        return REASON_CODE_SCHEMA_VERSION

    def get(self, code: str) -> ReasonCode | None:
        return self._by_code.get(code)

    def __iter__(self) -> Iterator[ReasonCode]:
        return iter(self._entries)

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and code in self._by_code

    def __len__(self) -> int:
        return len(self._entries)

    def as_dicts(self) -> list[dict[str, str | int]]:
        """Return the registry as a JSON-friendly list of dicts."""

        return [
            {
                "code": entry.code,
                "actor": entry.actor,
                "severity": entry.severity,
                "description": entry.description,
            }
            for entry in self._entries
        ]


# Convenience singleton — most call sites only need the lookup.
DEFAULT_REGISTRY = ReasonCodeRegistry()


def render_frontend_table_ts(
    registry: ReasonCodeRegistry = DEFAULT_REGISTRY,
) -> str:
    """Render the registry as a TypeScript module body.

    The output mirrors :meth:`ReasonCodeRegistry.as_dicts` so the frontend
    can ``import { REASON_CODES, REASON_CODE_SCHEMA_VERSION } from ...``
    and feed straight into its i18n layer. The file is intentionally
    machine-generated — manual edits get clobbered.

    The emitted style matches the frontend biome config (double-quoted
    strings, unquoted object keys, trailing commas in multiline
    aggregates) so ``pnpm biome check .`` stays green on the generated
    file. The snapshot test
    :func:`tests.test_validation_reason_codes.test_generated_frontend_table_exists_and_matches_source_of_truth`
    asserts this byte-for-byte; any divergence between the renderer here
    and the biome formatter is a CI failure on both sides.
    """

    entries = registry.as_dicts()
    rendered_entries = _render_entries_block(entries)
    return (
        "// AUTOGENERATED by backend/src/bob/validation/reason_codes.py — do not edit.\n"
        "// Run the matching pytest to regenerate after editing the Python registry.\n"
        f"export const REASON_CODE_SCHEMA_VERSION = {registry.schema_version};\n\n"
        'export type ReasonCodeActor = "jarvis" | "sub_agent" | "shared";\n'
        'export type ReasonCodeSeverity = "info" | "warn" | "error";\n\n'
        "export interface ReasonCode {\n"
        "  code: string;\n"
        "  actor: ReasonCodeActor;\n"
        "  severity: ReasonCodeSeverity;\n"
        "  description: string;\n"
        "}\n\n"
        f"export const REASON_CODES: readonly ReasonCode[] = {rendered_entries};\n"
    )


def _render_entries_block(entries: list[dict[str, str | int]]) -> str:
    """Render the ``REASON_CODES`` literal in biome-compliant TS style.

    Why hand-roll this instead of ``json.dumps``? biome's default style
    drops the quotes around object keys and uses double-quoted strings
    + trailing commas in multiline arrays/objects. ``json.dumps`` emits
    valid JSON, but JSON is not TS source — keys MUST stay quoted and no
    trailing commas are allowed. We render a small TS object literal here
    so the snapshot test (which compares byte-for-byte against the
    on-disk file) stays in sync with what biome's formatter would write.
    """

    if not entries:
        return "[]"
    lines: list[str] = ["["]
    for entry in entries:
        lines.append("  {")
        for key in ("code", "actor", "severity", "description"):
            value = entry[key]
            # Escape backslashes + double-quotes so embedded quotes
            # round-trip. JSON's encoder handles both for us; we strip
            # the outer quotes off the JSON string and re-wrap in TS
            # double quotes (json.dumps emits ``"..."`` by default,
            # matching biome).
            escaped = json.dumps(value, ensure_ascii=False)
            lines.append(f"    {key}: {escaped},")
        lines.append("  },")
    lines.append("]")
    return "\n".join(lines)


def write_frontend_table(
    target: Path,
    registry: ReasonCodeRegistry = DEFAULT_REGISTRY,
) -> None:
    """Write the TypeScript table at ``target`` (overwrites existing file).

    Used by a test (and an optional CLI hook later) to keep the generated
    frontend file in sync with the Python source of truth. The test
    asserts the file on disk matches the rendered content byte-for-byte.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_frontend_table_ts(registry), encoding="utf-8")


__all__ = [
    "DEFAULT_REGISTRY",
    "REASON_CODE_SCHEMA_VERSION",
    "REASON_HARD_KILLED",
    "REASON_INVALID_OUTPUT",
    "REASON_ITERATION_CAP",
    "REASON_LLM_FAILED",
    "REASON_OK",
    "REASON_TOKEN_CAP",
    "REASON_TOOL_FAILED",
    "REASON_UNKNOWN_TASK",
    "REASON_USER_CANCELLED",
    "REASON_VALIDATION_EXHAUSTED",
    "REASON_WALL_CLOCK_CAP",
    "ReasonCode",
    "ReasonCodeActor",
    "ReasonCodeRegistry",
    "ReasonCodeSeverity",
    "render_frontend_table_ts",
    "write_frontend_table",
]
