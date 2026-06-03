// BobCard.tsx — Piste 3D · Nacre, the BOB thread card (PRD 0014 / issue 0085).
//
// The front card of the (future) thread deck (issue 0086 wraps a deck around
// it). It is the live fil of Bob's MAIN orchestrator turn, bound to the same
// real data the right-rail agent feed uses — keyed on the FIXED
// `agent_ref="jarvis"` (see `orchestrator.JARVIS_AGENT_REF`), exactly as
// sub-tasks bind to their own `task_id`. No props from the shell.
//
// Sections, faithful to the mockup `BobBody` (`Design Mockup/p3d-panels.jsx`)
// and screenshots `p3d-settings.png` / `01-piste.png`:
//   prompt  — the last user message (italic header; omitted on a proactive
//             synthesis that has no prompt).
//   Réflexion — the streamed `reasoning_delta` of the Jarvis lane when present;
//             otherwise a line narrated from the chip stream by the pure
//             `reflectionNarrator` (degraded / non-reasoning backend).
//   Tâches en arrière-plan — the live sub-tasks Bob invoked (name + tool +
//             state / ✓ rendu). Omitted entirely when Bob delegated nothing, so
//             a simple question stays an épuré thread.
//   Réponse — the synthesised reply, streamed live (`streamingAssistant.speech`)
//             then settled (`agent_answer` / the persisted assistant bubble),
//             rendered through the EXISTING markdown renderer (MarkdownView).
//   perf footer — real tok/s · ttft · ctx (the Jarvis `agent_perf` frame),
//             shown once the turn settles (phase `done`).
//
// Co-located styling: `BobCard.css` ports the relevant panel/Bob classes from
// `Design Mockup/p3d.css` (scoped under `.piste`).

import { reflectionNarrator } from "../../lib/reflectionNarrator";
import {
  type AgentPerf,
  type AgentTimelineItem,
  useActivityFeedStore,
} from "../../store/activityFeedStore";
import { useChatStore } from "../../store/chatStore";
import type { Task } from "../../types/ws";
import { MarkdownView } from "../MarkdownView";
import "./BobCard.css";

/** The fixed lane key the orchestrator tags Bob's main thread with — the mirror
 * of a sub-task's `task_id`. Pinned in `orchestrator.JARVIS_AGENT_REF`. */
const JARVIS_REF = "jarvis";

/** Bob's own phase chain (the mockup's `BOB_ORDER`), in the orchestrator's
 * vocabulary. Derived from real signals below, then drives the per-step
 * is-active / is-done classes and the head stat word. */
type BobPhase = "think" | "summon" | "wait" | "answer" | "done" | "error";

/** Phase → head stat word (mirrors the mockup's `BOB_STAT`). */
const BOB_STAT: Record<BobPhase, string> = {
  think: "réfléchit",
  summon: "invoque",
  wait: "tient le fil",
  answer: "répond",
  done: "au repos",
  error: "incident",
};

/** Sub-task `TaskState` → the short uppercase status word shown in the invoked
 * row (mirrors the mockup's `SUB_STAT`). `done` is rendered with a ✓ separately,
 * so it never reaches this map. */
const SUB_STAT: Record<Task["state"], string> = {
  pending: "en attente",
  running: "en cours",
  waiting_input: "attend",
  done: "rendu",
  failed: "échec",
};

/** A sub-task is settled-rendered when it reached `done` (✓ rendu in the row). */
function isRendered(state: Task["state"]): boolean {
  return state === "done";
}

/**
 * Derive Bob's card phase from the real turn signals. Honest mapping of the
 * mockup's chain onto what the orchestrator actually emits:
 *   - `error`  — the most recent settled assistant turn carries no answer but
 *                the turn ended after a failed delegation (any sub-task failed
 *                and there is no answer). Conservative; never blocks `done`.
 *   - `answer` — a reply is streaming OR settled while the turn is still active.
 *   - `done`   — the turn ended (`!waiting`) AND a reply has settled.
 *   - `wait`   — sub-tasks are in flight (delegated, not all returned).
 *   - `summon` — sub-tasks exist and were just spawned (none returned yet).
 *   - `think`  — default: Bob is reasoning (or about to delegate).
 */
function deriveBobPhase(args: {
  waiting: boolean;
  hasAnswer: boolean;
  answering: boolean;
  tasks: Task[];
}): BobPhase {
  const { waiting, hasAnswer, answering, tasks } = args;
  const anyRunning = tasks.some((t) => t.state === "running" || t.state === "pending");
  const allReturned = tasks.length > 0 && tasks.every((t) => t.state === "done");
  const anyFailed = tasks.some((t) => t.state === "failed");

  // Settled reply (live or done) is the strongest "répond/au repos" signal.
  if (answering || hasAnswer) {
    if (!waiting && hasAnswer) return "done";
    return "answer";
  }
  // No answer yet, turn ended with a failed delegation and nothing to show.
  if (!waiting && anyFailed && !allReturned) return "error";
  // Delegation in flight.
  if (tasks.length > 0) {
    if (anyRunning) return "wait";
    return "summon";
  }
  return "think";
}

export function BobCard() {
  // ── Chat-store signals ────────────────────────────────────────────────────
  const messages = useChatStore((s) => s.messages);
  const streamingAssistant = useChatStore((s) => s.streamingAssistant);
  const tasksMap = useChatStore((s) => s.tasks);
  const waiting = useChatStore((s) => s.isWaitingResponse);

  // ── Jarvis-lane signals (same store the right-rail feed reads) ─────────────
  const timeline = useActivityFeedStore((s) => s.timelineByAgent[JARVIS_REF]);
  const settledAnswer = useActivityFeedStore((s) => s.answerByAgent[JARVIS_REF]);
  const perf = useActivityFeedStore((s) => s.perfByAgent[JARVIS_REF]);

  // The thread's tail tells us the turn state: a trailing USER message means a
  // reply is still pending (don't surface a stale prior answer), a trailing
  // ASSISTANT message is the current settled reply / a proactive synthesis.
  const lastMessage = messages.length > 0 ? messages[messages.length - 1] : undefined;
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  // The prompt at the head of the card is the user message of the CURRENT turn.
  // A proactive Bob synthesis (slice #0021) leaves the assistant message last
  // with no fresh user turn → no prompt block, but the card still renders off
  // the answer. So only show a prompt while the user message is the tail (reply
  // pending) or it directly precedes the trailing assistant reply.
  const promptIsCurrent =
    lastMessage?.role === "user" ||
    (lastMessage?.role === "assistant" && !lastMessage.proactive && lastUser !== undefined);
  const prompt = promptIsCurrent ? (lastUser?.content ?? "") : "";

  // The streamed reply suffix while the turn is in flight; once it settles the
  // persisted assistant bubble (== the trailing message) carries the full
  // markdown. The dedicated `agent_answer` event is the same text — used as a
  // fallback when the bubble hasn't landed yet. Gating on the trailing-assistant
  // tail prevents a previous turn's answer from lingering under a fresh think.
  const streamingSpeech = streamingAssistant?.speech ?? "";
  const settledTurnAnswer =
    lastMessage?.role === "assistant" ? lastMessage.content || settledAnswer || "" : "";
  const answerText = streamingSpeech || settledTurnAnswer;
  const answering = streamingSpeech.length > 0;
  const hasAnswer = answerText.trim().length > 0;

  // Sub-tasks Bob invoked, in spawn order (createdAt). The store keys by id; we
  // order so the invoked list is stable as states flip.
  const tasks = Object.values(tasksMap).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  const hasTasks = tasks.length > 0;
  const returned = tasks.filter((t) => isRendered(t.state)).length;
  const allReturned = hasTasks && returned === tasks.length;

  // ── Réflexion: real reasoning primes, else narrated fallback ───────────────
  const reflection = reflectionNarrator(timeline);
  // Reasoning is actively streaming when the turn is in flight AND the trailing
  // lane item is reasoning text — drives the "en cours…" meta + the caret.
  const lastItem = timeline && timeline.length > 0 ? timeline[timeline.length - 1] : undefined;
  const reasoningStreaming =
    waiting && reflection.kind === "reasoning" && lastItem?.kind === "reasoning";

  const phase = deriveBobPhase({ waiting, hasAnswer, answering, tasks });

  // The card is meaningful when there is anything to show: a prompt, any lane
  // activity, or an answer (covers the proactive-synthesis-without-prompt case).
  const hasContent =
    prompt.length > 0 || hasAnswer || (timeline?.length ?? 0) > 0 || reflection.kind !== "empty";
  if (!hasContent) return null;

  const working = phase !== "done" && phase !== "error";
  const thinkActive = phase === "think";
  const summonActive = phase === "summon" || phase === "wait";
  const answerActive = phase === "answer";

  return (
    <div className="stack-card is-bob is-front">
      <div className="panel bob-panel">
        <div className="panel-head">
          <span className="bob-orb" data-live={working} />
          <span className="panel-title">BOB</span>
          <span className="bob-role">fil de conscience</span>
          <span className="panel-phase">{BOB_STAT[phase]}</span>
        </div>

        {prompt && <div className="task-prompt">{prompt}</div>}

        <div className="task-scroll">
          {/* RÉFLEXION — streamed reasoning, or narrated fallback */}
          {reflection.kind !== "empty" && (
            <section className={`task-step ${thinkActive ? "is-active" : "is-done"}`}>
              <div className="step-key">
                <span className="step-pip" />
                <span className="step-label">Réflexion</span>
                <span className="step-meta">{reasoningStreaming ? "en cours…" : "monologue"}</span>
              </div>
              <p className="think-body">
                {reflection.text}
                {reasoningStreaming && <span className="caret" />}
              </p>
            </section>
          )}

          {/* TÂCHES EN ARRIÈRE-PLAN — live sub-tasks (omitted when none) */}
          {hasTasks && (
            <section className={`task-step ${summonActive ? "is-active" : "is-done"}`}>
              <div className="step-key">
                <span className="step-pip" />
                <span className="step-label">Tâches en arrière-plan</span>
                <span className="step-meta">
                  {allReturned ? `${tasks.length} rendus` : `${returned}/${tasks.length} rendus`}
                </span>
              </div>
              <div className="invoked">
                {tasks.map((t) => (
                  <InvokedRow key={t.id} task={t} />
                ))}
              </div>
            </section>
          )}

          {/* RÉPONSE — streamed synthesis, markdown (existing renderer) */}
          {hasAnswer && (
            <section className={`task-step ${answerActive ? "is-active" : "is-done"}`}>
              <div className="step-key">
                <span className="step-pip" />
                <span className="step-label">Réponse</span>
              </div>
              <div className="answer-box">
                <MarkdownView props={{ content: answerText }} />
                {answering && <span className="caret caret-ink" />}
              </div>
            </section>
          )}

          {/* PERF — real tok/s · ttft · ctx, once the turn settles */}
          {phase === "done" && <PerfFooter perf={perf} />}
        </div>
      </div>
    </div>
  );
}

/** One « Tâches en arrière-plan » row: name + tool + state / ✓ rendu. The tool
 * name is read from the most recent `tool_call` chip on the sub-task's lane
 * (`agent_ref` = the task id); it falls back to a neutral "outil" label when the
 * chip's args/result were redacted or no chip arrived yet — the redaction
 * fallback still shows the task NAME + STATE, never an empty row. */
function InvokedRow({ task }: { task: Task }) {
  const timeline = useActivityFeedStore((s) => s.timelineByAgent[task.id]);
  const toolName = latestToolName(timeline);
  const rendered = isRendered(task.state);
  const live = task.state === "running" || task.state === "waiting_input";
  const cls = rendered ? "is-done" : live ? "is-live" : "is-pending";
  return (
    <div className={`invoked-row ${cls}`}>
      <span className="invoked-glyph">◇</span>
      <span className="invoked-name">{task.title}</span>
      <span className="invoked-tool">{toolName}</span>
      <span className="invoked-stat">
        {rendered ? (
          <>
            <span className="bgtask-chk">✓</span>rendu
          </>
        ) : (
          <>
            <span className="invoked-dot" />
            {SUB_STAT[task.state]}
          </>
        )}
      </span>
    </div>
  );
}

/** The label of the most recent `tool_call` chip on a sub-agent lane, or a
 * neutral placeholder when none has arrived yet (redacted / pre-tool). Chip
 * labels are redacted server-side, so this never leaks content. */
function latestToolName(timeline: AgentTimelineItem[] | undefined): string {
  if (timeline) {
    for (let i = timeline.length - 1; i >= 0; i--) {
      const it = timeline[i];
      if (it.kind === "chip" && it.activityKind === "tool_call") return it.label;
    }
  }
  return "outil";
}

/** Perf footer — real token throughput + timing + context, rendered only for
 * the fields the backend actually reported (a degraded backend emits no
 * `agent_perf` at all, so the footer simply doesn't appear). Mirrors the
 * mockup's `task-perf` (tok/s · ttft · ctx). */
function PerfFooter({ perf }: { perf: AgentPerf | undefined }) {
  if (!perf) return null;
  const items: Array<[string, string]> = [];
  if (perf.tokS != null) items.push([`${perf.tokS}`, "tok/s"]);
  if (perf.ttftS != null) items.push([`${perf.ttftS}s`, "ttft"]);
  if (perf.tokensIn != null) items.push([formatCtx(perf.tokensIn), "ctx"]);
  if (items.length === 0) return null;
  return (
    <div className="task-perf">
      {items.map(([v, label]) => (
        <span key={label}>
          <b>{v}</b> {label}
        </span>
      ))}
    </div>
  );
}

/** Compact a context-token count the way the mockup writes it (`9.4k`). Values
 * under 1000 are shown verbatim. */
function formatCtx(tokens: number): string {
  if (tokens < 1000) return `${tokens}`;
  const k = tokens / 1000;
  return `${k >= 10 ? Math.round(k) : k.toFixed(1)}k`;
}
