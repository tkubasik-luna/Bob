// SubCard.tsx — Piste 3D · Nacre, ONE sub-task card in the thread deck
// (PRD 0014 / issue 0086).
//
// Bob summons sub-tasks; each one becomes a card stacked BEHIND the BOB card in
// the left slot. A SubCard is the lavender twin of `BobCard`: it renders the
// REAL sub-agent fil — réflexion → appel d'OUTIL (name + args + résultat) →
// rendu (↳) — bound to the same stores the right-rail agent feed reads, keyed on
// the sub-task id (== its `agent_ref`), exactly as `BobCard` binds to "jarvis".
//
// Faithful to the mockup `SubBody` / `DeckCard` (sub branch) in
// `Design Mockup/p3d-panels.jsx` and the deck CSS in `Design Mockup/p3d.css`:
//   chrome  — secondary lavender panel (`.sub-panel`), ◇ glyph, « par BOB » tag.
//   front   — full body: sub-think / sub-tool / sub-ret.
//   back    — collapsed to a header tab + a `sub-spec-line` (goal · tool); the
//             body is hidden by CSS (`.stack-card.is-back .sub-body`).
//
// This component renders ONLY the card's inner `.panel.sub-panel`; the deck
// wrapper (`.stack-card` + rank transform + click→promote) is owned by
// `TaskSlot`. Co-located styling: `SubCard.css`.
//
// Redaction note (acceptance criterion): the tool's args/result are redacted
// server-side and may be absent. The card always shows the tool NAME + the
// sub-task STATE; args/result render only when present — never an empty row.

import { useState } from "react";
import { deriveAgentPhase } from "../../lib/agentPhase";
import { buildSubFlow } from "../../lib/threadFlow";
import { type AgentTimelineItem, useActivityFeedStore } from "../../store/activityFeedStore";
import type { Task } from "../../types/ws";
import "./SubCard.css";

/** Sub-task `TaskState` → the short status word shown in the card head (mirrors
 * the mockup's `SUB_STAT`). `done` reads as « rendu ». */
const SUB_STAT: Record<Task["state"], string> = {
  pending: "en attente",
  running: "en cours",
  waiting_input: "attend",
  done: "rendu",
  failed: "échec",
};

/** A chip item, narrowed off the interleaved timeline. */
type ChipItem = Extract<AgentTimelineItem, { kind: "chip" }>;

/** The most recent `tool_call` chip on a sub-agent lane — the call the card
 * surfaces (name + args + result). Undefined when none has arrived (pre-tool /
 * a task that never called a tool). */
function latestToolChip(timeline: AgentTimelineItem[] | undefined): ChipItem | undefined {
  if (!timeline) return undefined;
  for (let i = timeline.length - 1; i >= 0; i--) {
    const it = timeline[i];
    if (it.kind === "chip" && it.activityKind === "tool_call") return it;
  }
  return undefined;
}

export function SubCard({
  task,
  front,
  pinned = false,
}: { task: Task; front: boolean; pinned?: boolean }) {
  // Same store the right-rail feed + InvokedRow read, keyed on the sub-task id.
  const timeline = useActivityFeedStore((s) => s.timelineByAgent[task.id]);
  const laneAnswer = useActivityFeedStore((s) => s.answerByAgent[task.id]);

  const phase = deriveAgentPhase(timeline, terminalState(task.state));
  const working = phase.status === "running";

  // ── Chronological body (PRD 0014): réflexion runs + each tool call, in
  // arrival order — no longer collapsed to the single latest tool. ────────────
  const flow = buildSubFlow(timeline);
  let lastReflectionIdx = -1;
  let lastToolIdx = -1;
  flow.forEach((node, i) => {
    if (node.kind === "reflection") lastReflectionIdx = i;
    else lastToolIdx = i;
  });

  // The collapsed (back) tab still summarises with the latest tool's name; a
  // redaction / pre-tool fallback names it neutrally so the row is never empty.
  const toolChip = latestToolChip(timeline);
  const toolName = toolChip?.label ?? "outil";
  const toolShown = toolChip !== undefined;

  // ── Rendu — the sub-agent's settled reply (↳). The persisted task `result`
  // is the canonical handle; the lane `answer` is the live fallback before the
  // result frame lands. A `failed` task's `result` is a reason string, shown as
  // the rendu so the card always closes honestly. ──────────────────────────────
  const renduText = (task.result || laneAnswer || "").trim();
  const hasRendu = renduText.length > 0;

  return (
    <div className="panel sub-panel">
      <div className="panel-head">
        <span className="sub-glyph">◇</span>
        <span className="panel-title">{task.title}</span>
        <span className="sub-by">par&nbsp;BOB</span>
        {pinned && <span className="panel-pin">épinglé</span>}
        <span className="panel-phase">{SUB_STAT[task.state]}</span>
      </div>

      {/* COLLAPSED (back) tab — what the card is about: goal · tool. Hidden by
          CSS on the front card. */}
      {!front && (
        <div className="sub-spec-line">{collapsedSpec(task.goal, toolName, toolShown)}</div>
      )}

      <div className="sub-body">
        {/* CHRONOLOGICAL FLOW — réflexion runs + each tool call, in arrival
            order (PRD 0014). A running call shows « appel… »; a settled one
            settles in place with its redacted args/result. */}
        {flow.map((node, i) => {
          if (node.kind === "reflection") {
            const streaming = working && node.source === "reasoning" && i === lastReflectionIdx;
            return (
              <p key={`${node.kind}-${i}`} className="sub-think">
                {node.text}
                {streaming && <span className="caret" />}
              </p>
            );
          }
          const chip = node.chip;
          // The tool-retrieval gate is a neutral one-shot marker (no running →
          // settled pair), so it always reads as settled — never « appel… ».
          const isRetrieval = chip.activityKind === "tool_retrieval";
          // Per-chip status; the tail tool also reads as settled once the task
          // itself is done (covers a finish chip that was redacted / never came).
          const toolOk =
            isRetrieval || chip.status === "ok" || (i === lastToolIdx && task.state === "done");
          return (
            <div
              key={`${node.kind}-${i}`}
              className={`sub-tool ${isRetrieval ? "is-retrieval" : toolOk ? "is-ok" : "is-run"}`}
            >
              <div className="sub-tool-line">
                <span className="sub-tool-mark" />
                <span className="sub-tool-name">{chip.label}</span>
                {!toolOk && <span className="sub-tool-run">appel…</span>}
              </div>
              {chip.args &&
                (isRetrieval ? (
                  <RetrievalArgs text={chip.args} />
                ) : (
                  <div className="sub-tool-args">{chip.args}</div>
                ))}
              {chip.result && (
                <div className="sub-tool-res">
                  <span className="bgtask-chk">✓</span>
                  {chip.result}
                </div>
              )}
            </div>
          );
        })}

        {/* RENDU — the sub-agent's settled reply (↳) */}
        {hasRendu && (
          <div className="sub-ret">
            <span className="sub-ret-arrow">↳</span>
            <span>{renduText}</span>
          </div>
        )}
      </div>
    </div>
  );
}

/** The tool-retrieval chip's args (advertised tools + scoreboard) can run long
 * once many tools are connected. Collapsed it stays a compact one-liner (CSS
 * ellipsis — the « … » the user wanted gone); click toggles to the full wrapped
 * list. `stopPropagation` so toggling never re-promotes the deck card. */
function RetrievalArgs({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      type="button"
      className={`sub-tool-args is-retrieval-args${expanded ? " is-expanded" : ""}`}
      aria-expanded={expanded}
      title={expanded ? "Réduire" : "Tout afficher"}
      onClick={(e) => {
        e.stopPropagation();
        setExpanded((v) => !v);
      }}
    >
      <span className="ret-args-text">{text}</span>
      <span className="ret-args-chev">{expanded ? "▾" : "▸"}</span>
    </button>
  );
}

/** Map the sub-task state onto the terminal handle `deriveAgentPhase` expects:
 * only `done` / `failed` are terminal; everything else is in-progress
 * (`undefined`). */
function terminalState(state: Task["state"]): "done" | "failed" | undefined {
  if (state === "done") return "done";
  if (state === "failed") return "failed";
  return undefined;
}

/** The collapsed back-card spec line: `goal · tool`. Falls back gracefully when
 * the goal is empty or no tool has been named yet, never rendering a stray
 * separator. Mirrors the mockup's `task.spec · task.tool.name`. */
function collapsedSpec(goal: string, toolName: string, toolShown: boolean): string {
  const left = goal.trim();
  const right = toolShown ? toolName : "";
  if (left && right) return `${left} · ${right}`;
  return left || right || "sous-tâche";
}
