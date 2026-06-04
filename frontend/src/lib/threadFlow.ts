import type { AgentTimelineItem } from "../store/activityFeedStore";
import type { Task } from "../types/ws";
import { reflectionNarrator } from "./reflectionNarrator";

/**
 * PRD 0014 — CHRONOLOGICAL thread body for the Piste 3D cards.
 *
 * The BOB / sub-task cards used to render their content in three FIXED, grouped
 * sections (Réflexion → Tâches/Outil → Réponse/Rendu): all reasoning hoisted to
 * the top, regardless of WHEN it streamed. The user wants the réflexion NOT
 * grouped — a thought that streamed AFTER a task was created must render AFTER
 * that task, in pure arrival order.
 *
 * The `activityFeedStore` timeline is ALREADY the chronological spine: a single
 * ordered `AgentTimelineItem[]` per `agent_ref` interleaving reasoning runs
 * (coalesced bursts of `reasoning_delta`) with activity chips (`agent_activity`),
 * exactly as they arrived on the wire. These two pure builders fold that spine
 * into the ordered render nodes each card maps over — so the card markup stays
 * thin and the ordering logic is unit-testable without React / the WS / the
 * store wiring.
 *
 * Correlation note (BOB card). A spawned sub-task surfaces on the Jarvis lane as
 * a `tool_call` chip the orchestrator emits via
 * `_jarvis_orchestration_chip("délègue", …)` (`backend/.../orchestrator.py`).
 * NO `task_id` rides that chip — only the redacted title in the label — but the
 * chips are emitted in `spawned` order, which is the same order the tasks are
 * created (`createdAt`). So the k-th « délègue » chip maps to the k-th
 * `createdAt`-sorted task. A `transmet` / `annule` chip acts on an EXISTING task
 * (no new spawn), so it must NOT consume a task slot — hence the verb-prefix
 * match. If that wording ever drifts, the trailing-leftover fallback still
 * surfaces every task (just un-interleaved), never dropping one.
 */

/** A chip item, narrowed off the interleaved timeline. */
type ChipItem = Extract<AgentTimelineItem, { kind: "chip" }>;

/** Where a réflexion node's text came from — drives the live caret / « en
 * cours… » meta (only the real streamed chain-of-thought is "live"; a narrated
 * fallback line is static). Mirrors `Reflection.kind` minus `empty`. */
export type ReflectionSource = "reasoning" | "narrated";

/** One ordered block in the BOB card body (the answer + perf footer are owned
 * by the component and always trail the flow). Consecutive delegations with no
 * reasoning between them coalesce into ONE `tasks` node so the « Tâches en
 * arrière-plan » grouping survives within a chronological run. */
export type BobFlowNode =
  | { kind: "reflection"; text: string; source: ReflectionSource }
  | { kind: "tasks"; tasks: Task[] };

/** One ordered block in a sub-task card body (the rendu ↳ is owned by the
 * component and always trails the flow). Each tool call is its OWN node — the
 * card no longer collapses to the single latest tool. */
export type SubFlowNode =
  | { kind: "reflection"; text: string; source: ReflectionSource }
  | { kind: "tool"; chip: ChipItem };

/** The tool variant of a sub-flow node (for in-place coalescing). */
type SubToolNode = Extract<SubFlowNode, { kind: "tool" }>;

/** Whether a `tool_call` chip on the Jarvis lane is a SPAWN delegation (vs a
 * `transmet` / `annule` act on an existing task). See the correlation note.
 * Exported so the per-turn conversation splitter can count delegations to scope
 * each turn's tasks. */
export function isDelegateChip(chip: ChipItem): boolean {
  return chip.activityKind === "tool_call" && chip.label.startsWith("délègue");
}

/** A reasoning run carries content once it has a non-blank token (a stray
 * leading newline never opens a block — matches `reflectionNarrator`). */
function isContentfulReasoning(item: AgentTimelineItem): boolean {
  return item.kind === "reasoning" && item.text.trim().length > 0;
}

/** The most recent still-`running` tool node for `label` (none yet settled), so
 * a settling chip can replace it in place rather than open a second block. */
function lastOpenToolNode(nodes: SubFlowNode[], label: string): SubToolNode | undefined {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    if (n.kind === "tool" && n.chip.status === "running" && n.chip.label === label) return n;
  }
  return undefined;
}

/**
 * Fold the Jarvis lane timeline + the spawned sub-tasks into the BOB card's
 * chronological body nodes.
 *
 * Walk the timeline in arrival order:
 *   - a contentful reasoning run opens a `reflection` node (and closes any open
 *     run of tasks, so a thought after a delegation lands after it);
 *   - a « délègue » chip appends the next `createdAt`-sorted task to the current
 *     tasks run (coalescing consecutive delegations).
 * Then: any tasks with no matching chip (rehydrate-on-reload / redaction /
 * chip-not-yet-arrived race) trail as one final group, and — for a degraded
 * backend with NO reasoning channel — the narrated `reflectionNarrator` line is
 * prepended so the réflexion is never blank (nothing to interleave there).
 *
 * Pure; never mutates its inputs.
 */
export function buildBobFlow(
  timeline: readonly AgentTimelineItem[] | undefined,
  tasks: readonly Task[],
): BobFlowNode[] {
  const items = timeline ?? [];
  // `createdAt` order == `spawned` order == « délègue » chip order.
  const sorted = [...tasks].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  const hasReasoning = items.some(isContentfulReasoning);

  const nodes: BobFlowNode[] = [];
  let cursor = 0;
  let pending: Task[] = [];
  const flushTasks = () => {
    if (pending.length > 0) {
      nodes.push({ kind: "tasks", tasks: pending });
      pending = [];
    }
  };

  for (const item of items) {
    if (item.kind === "reasoning") {
      if (!isContentfulReasoning(item)) continue;
      flushTasks();
      nodes.push({ kind: "reflection", text: item.text, source: "reasoning" });
    } else if (isDelegateChip(item) && cursor < sorted.length) {
      pending.push(sorted[cursor++]);
    }
  }
  flushTasks();

  // Tasks with no « délègue » chip to anchor them (rehydrated lanes carry no
  // replayed timeline) trail as one group so a reconnect still lists them.
  if (cursor < sorted.length) {
    nodes.push({ kind: "tasks", tasks: sorted.slice(cursor) });
  }

  // Degraded backend (Claude CLI bridge / non-reasoning model): no reasoning to
  // interleave — keep the single narrated réflexion line at the head.
  if (!hasReasoning) {
    const narrated = reflectionNarrator(items);
    if (narrated.kind !== "empty") {
      nodes.unshift({ kind: "reflection", text: narrated.text, source: "narrated" });
    }
  }

  return nodes;
}

/**
 * Fold a sub-agent lane timeline into the sub-task card's chronological body
 * nodes: each contentful reasoning run is a `reflection` node, each `tool_call`
 * chip its OWN `tool` node, in arrival order (so réflexion → outil → réflexion →
 * outil reads as it happened, not collapsed to the latest tool). A degraded
 * backend with neither reasoning nor a tool call falls back to the single
 * narrated line so the body is never blank. Pure; never mutates its input.
 */
export function buildSubFlow(timeline: readonly AgentTimelineItem[] | undefined): SubFlowNode[] {
  const items = timeline ?? [];
  const nodes: SubFlowNode[] = [];

  for (const item of items) {
    if (item.kind === "reasoning") {
      if (!isContentfulReasoning(item)) continue;
      nodes.push({ kind: "reflection", text: item.text, source: "reasoning" });
    } else if (item.kind === "chip" && item.activityKind === "tool_call") {
      // A sub-agent emits TWO chips per tool call — a `running` start (args only)
      // then a settled `ok`/`error` (args + result). Coalesce: a settled chip
      // REPLACES the most recent open `running` node of the same tool, so the
      // call renders once (settling in place) instead of as two blocks.
      // (backend `sub_agent/runner.py` ToolCallStarted / ToolCallFinished.)
      if (item.status !== "running") {
        const open = lastOpenToolNode(nodes, item.label);
        if (open) {
          open.chip = item;
          continue;
        }
      }
      nodes.push({ kind: "tool", chip: item });
    } else if (item.kind === "chip" && item.activityKind === "tool_retrieval") {
      // PRD 0015 / issue 0092 — the goal-driven tool-retrieval gate. A single
      // neutral (`info`) chip emitted once per task before the first call; render
      // it inline like a tool node so the user sees which tools were advertised.
      nodes.push({ kind: "tool", chip: item });
    }
  }

  // Nothing concrete streamed (no reasoning, no tool call) — narrate from the
  // lifecycle chips so the card reads as working rather than blank.
  if (nodes.length === 0) {
    const narrated = reflectionNarrator(items);
    if (narrated.kind !== "empty") {
      nodes.push({ kind: "reflection", text: narrated.text, source: "narrated" });
    }
  }

  return nodes;
}
