import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { type AgentPhase, PHASE_META, deriveAgentPhase } from "../lib/agentPhase";
import { isAtBottom, shouldAutoScroll } from "../lib/autoScroll";
import {
  type AgentPerf,
  type AgentTimelineItem,
  useActivityFeedStore,
} from "../store/activityFeedStore";
import { useChatStore } from "../store/chatStore";
import type { AgentActivityStatus } from "../types/ws";
import { MarkdownView } from "./MarkdownView";

type Props = {
  /** The running sub-task's id — matches the `agent_ref` on `reasoning_delta`,
   * `agent_activity` and `agent_perf` events. `"jarvis"` for the orchestrator. */
  agentRef: string;
  /** Issue 0074 — collapsed-summary title (the sub-task's title; Jarvis has none). */
  title?: string;
  /** Issue 0074 — true when the task has a result the "résultat" button surfaces. */
  hasResult?: boolean;
  /** Issue 0074 — open the EXISTING result view (TaskDrawer / SectionsOverlay). */
  onOpenResult?: () => void;
};

const JARVIS_REF = "jarvis";

/** A stable lane tint for a sub-agent, derived from its id so the same agent
 * keeps its colour across renders. Jarvis uses the secondary accent. */
const SUB_TINTS = ["#82a4ae", "#8fa585", "#c2a06a", "#a88ba2", "#8294b0"];
function laneTint(agentRef: string): string {
  if (agentRef === JARVIS_REF) return "var(--accent-2)";
  let h = 0;
  for (let i = 0; i < agentRef.length; i++) h = (h * 31 + agentRef.charCodeAt(i)) >>> 0;
  return SUB_TINTS[h % SUB_TINTS.length];
}

/** Chip status → glyph. The mockup's diamond mark is coloured by CSS class. */
const CHIP_GLYPH: Record<AgentActivityStatus, string> = {
  running: "",
  ok: "✓",
  error: "✗",
  warn: "▲",
  info: "•",
};

/**
 * Reasoning-streaming PRD — per-agent lane, ported from the Design-Mockup
 * `AgentLane`. Renders the HONEST signal set Bob has on `/v1` (see
 * `lib/agentPhase`): a derived PHASE row (indeterminate spinner, never a % bar —
 * the load/prompt-progress bars are native-only and not available), a
 * collapsible THINKING monologue (the `reasoning_delta` stream — degraded agents
 * fall back to their narrated `progress` thought on the same channel), TOOL
 * CHIPS (the `agent_activity` timeline — label + status; args/result are a later
 * backend slice), a PERF footer (`agent_perf`) and an inline ERROR.
 *
 * Lifecycle (issue 0074): while running it streams; on a terminal state it
 * collapses to a one-line summary + a "résultat" button (opens the existing
 * result view) + an expand affordance that re-shows the full body. The timeline
 * + perf are RETAINED across the terminal transition so expand can re-read them.
 *
 * Renders nothing for a never-started, never-finished agent.
 */
export function AgentBlock({ agentRef, title, hasResult, onOpenResult }: Props) {
  const timeline = useActivityFeedStore((s) => s.timelineByAgent[agentRef]);
  const finalState = useActivityFeedStore((s) => s.finishedByAgent[agentRef]);
  const perf = useActivityFeedStore((s) => s.perfByAgent[agentRef]);
  const answer = useActivityFeedStore((s) => s.answerByAgent[agentRef]);
  // Jarvis has no `task`/terminal state — its turn is bracketed by the
  // `thinking` WS event (chat store `isWaitingResponse`). Used to settle the
  // persistent lane to `done` between turns instead of hanging on "Réflexion".
  const isWaiting = useChatStore((s) => s.isWaitingResponse);

  const [expanded, setExpanded] = useState(false);
  const [thinkOpen, setThinkOpen] = useState<boolean | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stuckToBottomRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (el) stuckToBottomRef.current = isAtBottom(el);
  }, []);

  const itemCount = timeline?.length ?? 0;
  const lastText = timeline && itemCount > 0 ? JSON.stringify(timeline[itemCount - 1]) : "";
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-pin when the trailing item grows (lastText) or count changes; refs are intentionally not deps.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (shouldAutoScroll({ stuckToBottom: stuckToBottomRef.current, expanded })) {
      el.scrollTop = el.scrollHeight;
    }
  }, [itemCount, lastText, expanded]);

  const finished = finalState === "done" || finalState === "failed";
  const hasTimeline = !!timeline && timeline.length > 0;
  if (!finished && !hasTimeline) return null;

  const isJarvis = agentRef === JARVIS_REF;
  const phase = deriveAgentPhase(
    timeline,
    finalState,
    isJarvis ? { turnActive: isWaiting } : undefined,
  );
  const tint = laneTint(agentRef);
  const name = isJarvis ? "JARVIS" : (title ?? agentRef);
  const role = isJarvis ? "orchestrator" : "sub-agent";

  // Split the interleaved timeline into the mockup's zones: a thinking monologue
  // (reasoning runs concatenated) and the tool chips.
  const thinkingText = (timeline ?? [])
    .filter(
      (it): it is Extract<AgentTimelineItem, { kind: "reasoning" }> => it.kind === "reasoning",
    )
    .map((it) => it.text)
    .join("");
  const chips = (timeline ?? []).filter(
    (it): it is Extract<AgentTimelineItem, { kind: "chip" }> => it.kind === "chip",
  );
  const streaming =
    phase.status === "running" &&
    !!timeline &&
    timeline.length > 0 &&
    timeline[timeline.length - 1].kind === "reasoning";

  // ── COLLAPSED summary (issue 0074) ───────────────────────────────────────
  if (finished && !expanded) {
    const summary =
      finalState === "failed"
        ? thinkingText.split("\n").pop() || "Échec"
        : thinkingText.split("\n").find((l) => l.trim()) || title || "Terminé";
    return (
      <div
        className={`agent-lane is-${isJarvis ? "jarvis" : "sub"} status-${phase.status} is-folded`}
        style={{ ["--lane-tint" as string]: tint }}
      >
        <button type="button" className="al-head" onClick={() => setExpanded(true)}>
          <span className="al-glyph">{isJarvis ? "⌬" : "◇"}</span>
          <span className="al-name">{name}</span>
          <span className="al-role">{role}</span>
          <span className="al-chev">▸</span>
        </button>
        <PhaseRow phase={phase} />
        <div className={`al-fold ${finalState === "failed" ? "is-err" : ""}`}>{summary}</div>
        {hasResult && onOpenResult && (
          <button type="button" className="al-result" onClick={onOpenResult}>
            résultat
          </button>
        )}
      </div>
    );
  }

  // ── ACTIVE / expanded lane ───────────────────────────────────────────────
  const thinkExpanded = thinkOpen === null ? !finished : thinkOpen;
  return (
    <div
      className={`agent-lane is-${isJarvis ? "jarvis" : "sub"} status-${phase.status}`}
      style={{ ["--lane-tint" as string]: tint }}
    >
      <button
        type="button"
        className="al-head"
        onClick={() => (finished ? setExpanded(false) : undefined)}
      >
        <span className="al-glyph">{isJarvis ? "⌬" : "◇"}</span>
        <span className="al-name">{name}</span>
        <span className="al-role">{role}</span>
        {finished && <span className="al-chev">▾</span>}
      </button>

      <PhaseRow phase={phase} />

      <div className="al-body">
        {/* No reasoning channel (guided-JSON sub-agent — schema suppresses it)
            and nothing narrated yet: keep the lane alive with a status line so
            it never looks dead during the slow generation gap. */}
        {!thinkingText && phase.status === "running" && (
          <div className="al-status">
            <span className="al-status-dot" />
            <span>{PHASE_META[phase.key].label}</span>
          </div>
        )}
        {thinkingText && (
          <div className="al-think">
            <button
              type="button"
              className="al-think-head"
              onClick={() => setThinkOpen(!thinkExpanded)}
            >
              <span className="al-think-label">Réflexion</span>
              <span className="al-think-count">
                {streaming ? "streaming…" : `${countWords(thinkingText)} mots`}
              </span>
              <span className="al-think-toggle">{thinkExpanded ? "masquer" : "voir"}</span>
            </button>
            {thinkExpanded && (
              <div ref={scrollRef} onScroll={onScroll} className="al-think-body">
                {thinkingText}
                {streaming && <span className="al-caret" />}
              </div>
            )}
          </div>
        )}

        {chips.length > 0 && (
          <div className="al-tools">
            {chips.map((c, i) => (
              <div
                key={`${c.label}-${i}`}
                className={`al-chip ${
                  c.status === "running" ? "is-run" : c.status === "error" ? "is-fail" : "is-ok"
                }`}
                title={c.activityKind}
              >
                <div className="al-chip-head">
                  <span className="al-chip-mark" />
                  <span className="al-chip-name">{c.label}</span>
                  {c.status === "running" && <span className="al-chip-run">running</span>}
                </div>
                {c.args && <div className="al-chip-args">{c.args}</div>}
                {(c.result || (c.status !== "running" && CHIP_GLYPH[c.status])) && (
                  <div className="al-chip-result">
                    {c.status !== "running" && CHIP_GLYPH[c.status] && (
                      <span className="al-chip-glyph">{CHIP_GLYPH[c.status]}</span>
                    )}
                    {c.result && <span>{c.result}</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {finalState === "failed" && (
          <div className="al-error">
            <span className="al-error-glyph">✗</span>
            <span>{thinkingText.split("\n").pop() || "Le sous-agent a échoué."}</span>
          </div>
        )}

        {answer && (
          <div className="al-answer">
            <div className="al-answer-cap">Réponse</div>
            <MarkdownView props={{ content: answer }} />
          </div>
        )}

        <PerfFooter perf={perf} />

        {finished && hasResult && onOpenResult && (
          <button type="button" className="al-result" onClick={onOpenResult}>
            résultat
          </button>
        )}
      </div>
    </div>
  );
}

/** Phase row: animated pip + label + right marker + indeterminate spinner
 * (running) or a settled bar (done/error). No % bar — that signal is native-only. */
function PhaseRow({ phase }: { phase: AgentPhase }) {
  const meta = PHASE_META[phase.key];
  const right = phase.status === "done" ? "complete" : phase.status === "error" ? "fault" : "";
  return (
    <div className="al-phase" style={{ ["--phase-tint" as string]: meta.tint }}>
      <span className={`al-pip is-${phase.key}`} />
      <span className="al-phase-label">{meta.label}</span>
      <span className="al-phase-right">{right}</span>
      {phase.status === "running" ? (
        <span className="al-prog al-prog-indef">
          <span className="al-prog-sweep" />
        </span>
      ) : (
        <span className="al-prog al-prog-done" />
      )}
    </div>
  );
}

/** Perf footer — token usage + timing, rendered only for the fields present. */
function PerfFooter({ perf }: { perf: AgentPerf | undefined }) {
  if (!perf) return null;
  const items: Array<[string, string]> = [];
  if (perf.tokS != null) items.push([`${perf.tokS}`, "tok/s"]);
  if (perf.ttftS != null) items.push([`${perf.ttftS}s`, "ttft"]);
  if (perf.reasoningTokens) items.push([`${perf.reasoningTokens}`, "think"]);
  if (perf.tokensIn != null) items.push([`${perf.tokensIn}`, "ctx"]);
  if (items.length === 0) return null;
  return (
    <div className="al-perf">
      {items.map(([v, label]) => (
        <span key={label}>
          <b>{v}</b> {label}
        </span>
      ))}
    </div>
  );
}

function countWords(s: string): number {
  const t = s.trim();
  return t ? t.split(/\s+/).length : 0;
}
