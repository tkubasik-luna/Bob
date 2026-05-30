"""Per-run tool-result store (blackboard) + deterministic projections.

PRD 0009 — *Tool Result Store & Deterministic Deliverable Projection*.

A weak local model in the sub-agent control loop fails the
``request → tool → display`` flow two ways at once (see the 2026-05-30
mail-overlay investigation):

1. the full tool-result blob is replayed into every prompt, so the model
   drowns in its own transcript and loses track that the answer is already
   in hand (it then spins ``progress`` and never emits ``done``);
2. the structured deliverable (a Mail card) depends on the model emitting a
   perfect ``done`` payload — so on any forced exit the card dies even though
   the data exists.

This module is the architectural fix for both. It is a **blackboard**: each
successful tool result is written to a per-run store keyed by a short,
human-readable ``ref`` (``"gmail_search#1"``). Writing also runs the tool's
**projector** — a pure ``result -> ProjectedResult`` function owning the three
projections of that result:

- ``digest`` — a *compact* preview that goes into the ``tool`` transcript
  message instead of the full blob (fixes #1, the context bloat);
- ``deliverable`` — the ``{component, props}`` UI descriptor, built by code
  from data that already exists, so it survives *every* termination path
  (fixes #2 — the model is removed from the display-critical path);
- ``summary`` — a deterministic spoken/markdown summary;

plus ``terminal`` — whether this result is itself a complete answer (a
single-shot lookup like a mail search), which lets the runner *converge*
deterministically instead of waiting for the weak model to conclude.

The store is intentionally tiny and dependency-free (no pydantic, no
``ui_registry`` import): the runner validates a projected ``deliverable``
against the single ``ui_registry`` schema at the boundary. Keeping projection
*data* here and validation *there* mirrors the OpenClaw rule the codec layer
already follows — "core owns the loop; providers own the runtime hooks."
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Cap on the JSON-serialised ``summary`` produced by :func:`default_projector`
#: so a tool with no custom projector cannot fold a megabyte of result text
#: into a degraded ``done``'s ``result_summary`` and blow up the synthesis
#: prompt. Matches the runner's historical salvage cap.
_DEFAULT_SUMMARY_MAX_CHARS = 2000


@dataclass(frozen=True)
class ProjectedResult:
    """The deterministic projections of one tool result.

    Produced by a :data:`ToolResultProjector`. Every field has a safe default
    so a projector may populate only what it has.

    - ``digest`` — the compact form injected into the ``tool`` transcript
      message (context saver). Must omit anything large or privacy-sensitive
      (e.g. a Gmail ``bodyPreview``). Defaults to the empty dict.
    - ``deliverable`` — a **list** of ``{component, props}`` UI descriptors
      (PRD 0010 / issue 0066), or ``None`` when the result has nothing to render
      (e.g. an empty search). A single card is a list-of-one. Each descriptor is
      validated against ``ui_registry`` by the runner before it is shipped. An
      empty list is normalised to ``None`` by :meth:`__post_init__` so "nothing
      to render" has exactly one representation.
    - ``summary`` — a deterministic, human-facing summary used for the spoken
      ``result_summary`` when the runner finalises from the store.
    - ``terminal`` — ``True`` when this result is a complete answer, so the
      runner may converge (force ``done``) on it rather than waiting for the
      model. Single-shot tools (a mail search) set this; multi-step tools do
      not. The default projector always returns ``False``.
    """

    digest: dict[str, Any] = field(default_factory=dict)
    deliverable: list[dict[str, Any]] | None = None
    summary: str = ""
    terminal: bool = False

    def __post_init__(self) -> None:
        # Normalise an empty list to ``None`` so "no deliverable" has a single
        # canonical representation across the whole pipeline (PRD 0010 / 0066).
        # ``object.__setattr__`` because the dataclass is frozen.
        if self.deliverable is not None and len(self.deliverable) == 0:
            object.__setattr__(self, "deliverable", None)


#: A projector turns a raw tool-result dict into its :class:`ProjectedResult`.
#: It MUST be pure (no I/O) and SHOULD be total — but a raising projector is
#: tolerated by :meth:`ToolResultStore.put` (it falls back to the default), so
#: a buggy projector can never break a run.
ToolResultProjector = Callable[[dict[str, Any]], ProjectedResult]


def default_projector(result: dict[str, Any]) -> ProjectedResult:
    """Projection for a tool with no custom projector — preserves prior behaviour.

    ``digest`` is the result verbatim (so the transcript message is byte-for-byte
    what the runner stored before PRD 0009 — zero regression for un-projected
    tools), there is no structured ``deliverable``, the result is never
    ``terminal`` (un-projected tools never trigger convergence), and ``summary``
    is the compact JSON the runner's old ``_salvage_tool_result_text`` produced,
    capped at :data:`_DEFAULT_SUMMARY_MAX_CHARS`.
    """

    try:
        summary = json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        summary = repr(result)
    if len(summary) > _DEFAULT_SUMMARY_MAX_CHARS:
        summary = summary[:_DEFAULT_SUMMARY_MAX_CHARS] + "…"
    return ProjectedResult(digest=result, deliverable=None, summary=summary, terminal=False)


@dataclass(frozen=True)
class StoredResult:
    """One successful tool result on the blackboard.

    ``result`` is the FULL raw result — kept server-side only (it never enters
    the model's transcript; the ``projection.digest`` does). ``ref`` is the
    stable handle the model and the runner pass instead of the payload.
    """

    ref: str
    tool_name: str
    tool_version: str | None
    result: dict[str, Any]
    projection: ProjectedResult


class ToolResultStore:
    """In-memory, per-sub-agent-run store of successful tool results.

    Scope is one :meth:`SubAgentRunner._run`. Refs are ``f"{tool_name}#{n}"``
    with ``n`` a per-tool 1-based counter, so they are human-readable, stable,
    and greppable in logs (``gmail_search#1``). The store holds only results we
    chose to ``put`` — the runner only puts *successful* dispatches — so
    :meth:`last` is always the most recent usable result.

    Not thread-safe by design: a single run drives it sequentially.
    """

    def __init__(self) -> None:
        self._by_ref: dict[str, StoredResult] = {}
        self._counts: dict[str, int] = {}
        self._order: list[str] = []

    def put(
        self,
        *,
        tool_name: str,
        tool_version: str | None,
        result: dict[str, Any],
        projector: ToolResultProjector | None = None,
    ) -> StoredResult:
        """Store a successful tool result and return its :class:`StoredResult`.

        Runs ``projector`` (or :func:`default_projector`) to derive the
        digest / deliverable / summary / terminal projections. A projector that
        raises is caught and downgraded to :func:`default_projector` so a buggy
        projection can never abort the run — the worst case is "behaves like an
        un-projected tool this turn."
        """

        count = self._counts.get(tool_name, 0) + 1
        self._counts[tool_name] = count
        ref = f"{tool_name}#{count}"

        chosen = projector or default_projector
        try:
            projection = chosen(result)
        except Exception:
            # A buggy projector must never abort the run — degrade to the
            # default projection (behaves like an un-projected tool this turn).
            projection = default_projector(result)

        stored = StoredResult(
            ref=ref,
            tool_name=tool_name,
            tool_version=tool_version,
            result=result,
            projection=projection,
        )
        self._by_ref[ref] = stored
        self._order.append(ref)
        return stored

    def get(self, ref: str | None) -> StoredResult | None:
        """Resolve a ref to its :class:`StoredResult`; ``None`` if absent/empty."""

        if not ref:
            return None
        return self._by_ref.get(ref)

    def last(self) -> StoredResult | None:
        """The most recently stored result, or ``None`` when the store is empty."""

        if not self._order:
            return None
        return self._by_ref[self._order[-1]]

    def __len__(self) -> int:
        return len(self._by_ref)


__all__ = [
    "ProjectedResult",
    "StoredResult",
    "ToolResultProjector",
    "ToolResultStore",
    "default_projector",
]
