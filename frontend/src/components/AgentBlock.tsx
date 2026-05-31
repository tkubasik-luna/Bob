import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { isAtBottom, shouldAutoScroll } from "../lib/autoScroll";
import { type AgentTimelineItem, useActivityFeedStore } from "../store/activityFeedStore";
import type { AgentActivityStatus } from "../types/ws";

type Props = {
  /** The running sub-task's id — matches the `agent_ref` on `reasoning_delta`
   * and `agent_activity` events. */
  agentRef: string;
  /** Issue 0074 — collapsed-summary affordances. The mount context (TaskCard)
   * owns the task, so the summary's TITLE and the RESULT open path are passed
   * in rather than duplicated in the store. */
  title?: string;
  /** Issue 0074 — true when the task has a result the "résultat" button can
   * surface. When false the button is hidden (e.g. a bare failure). */
  hasResult?: boolean;
  /** Issue 0074 — open the EXISTING result view for this agent's task. Wired by
   * TaskCard to the very same `onOpen(task)` → `openTask(id)` path the card body
   * click uses, which opens the TaskDrawer (Objectif / Résultat / Historique).
   * Reuses the existing open path — does NOT reimplement any overlay. */
  onOpenResult?: () => void;
};

/** Minimal status → glyph + colour mapping for a chip. Kept tiny and
 * dependency-free (no icon library) — the chip is observability, not chrome. */
const STATUS_STYLE: Record<AgentActivityStatus, { glyph: string; className: string }> = {
  running: { glyph: "◌", className: "border-blue-700/50 bg-blue-900/30 text-blue-200/90" },
  ok: { glyph: "✓", className: "border-emerald-700/50 bg-emerald-900/30 text-emerald-200/90" },
  error: { glyph: "✕", className: "border-rose-700/50 bg-rose-900/30 text-rose-200/90" },
  warn: { glyph: "▲", className: "border-amber-700/50 bg-amber-900/30 text-amber-200/90" },
  info: { glyph: "•", className: "border-slate-600/50 bg-slate-800/40 text-slate-300/90" },
};

/** Issue 0074 — terminal-state badge for the collapsed summary. Only the two
 * terminal `TaskState`s reach here (the bridge marks finished on done / failed,
 * collapsing degraded / timeout / force-terminate onto `failed`). */
const FINAL_BADGE: Record<"done" | "failed", { label: string; className: string }> = {
  done: {
    label: "Terminée",
    className: "border-emerald-700/50 bg-emerald-900/30 text-emerald-200",
  },
  failed: { label: "Échec", className: "border-rose-700/50 bg-rose-900/30 text-rose-200" },
};

/**
 * PRD 0011 — agent-activity block.
 *
 * Issue 0069 rendered only the live streaming reasoning. Issue 0071 renders the
 * full per-agent timeline: reasoning text segments and discrete activity chips
 * INTERLEAVED in the exact chronological order they arrived (the store keeps an
 * ordered `AgentTimelineItem[]`). Chips are inline in the same flow as the
 * reasoning — NOT a separate zone (PRD 0011 decision) — shown as a small
 * icon + label coloured by status.
 *
 * Issue 0075 — SLIDING WINDOW for the ACTIVE block. A long reasoning must not
 * take over the screen, so this block is bounded-height with a scroll window
 * that AUTO-SCROLLS to the latest tokens as deltas arrive. If the user scrolls
 * up to read back, auto-scroll PAUSES until they return to the bottom (standard
 * chat-log behaviour). A "voir tout" toggle expands the block to its full
 * height; toggling back restores the bounded window. The auto-scroll decision
 * is the pure helper in `lib/autoScroll` (unit-tested there).
 *
 * Issue 0074 — COLLAPSE LIFECYCLE. When the agent's task terminates the block
 * stops being the live ACTIVE timeline and becomes a COLLAPSED summary: title +
 * final-state badge + a "résultat" button (opens the EXISTING result view via
 * `onOpenResult`) + an "expand / relire la réflexion" affordance that re-shows
 * the FULL reasoning + chips timeline (the same full-timeline rendering 0075's
 * "voir tout" expands to). The finished bit comes from the store's
 * `finishedByAgent` map; the timeline arrays are retained across the terminal
 * transition so expand can re-read them. A failure (failed / cap / stall
 * force-terminate) collapses with the `Échec` badge.
 *
 * The Jarvis block (0072) and the side-panel rail (0076) are later issues.
 * Renders nothing until the first item arrives (a model with no reasoning
 * channel still surfaces its chips here once it acts) AND it isn't finished —
 * a finished agent with no timeline still shows its collapsed summary.
 */
export function AgentBlock({ agentRef, title, hasResult, onOpenResult }: Props) {
  const timeline = useActivityFeedStore((s) => s.timelineByAgent[agentRef]);
  const finalState = useActivityFeedStore((s) => s.finishedByAgent[agentRef]);

  const [expanded, setExpanded] = useState(false);
  /** True when the windowed (non-expanded) content actually overflows the
   * bounded height. The "voir tout" toggle is pointless on a short reasoning /
   * a one-line answer, so it only shows when there's genuinely more to reveal. */
  const [overflowing, setOverflowing] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  /** Latched intent: true while the user is pinned to the bottom (auto-scroll
   * follows new tokens), false once they scroll up to read back. Flips back to
   * true when they scroll down to the bottom again. Held in a ref so a scroll
   * event doesn't force a re-render — only the layout effect reads it. */
  const stuckToBottomRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stuckToBottomRef.current = isAtBottom(el);
  }, []);

  // After each render that added content, pin to the bottom IFF the user is
  // still stuck there and we're not expanded (don't fight a manual scroll).
  const itemCount = timeline?.length ?? 0;
  const lastText = timeline && itemCount > 0 ? JSON.stringify(timeline[itemCount - 1]) : "";
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-pin whenever the trailing item grows (lastText) or the count changes — those are the content signals; refs are intentionally not deps.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (shouldAutoScroll({ stuckToBottom: stuckToBottomRef.current, expanded })) {
      el.scrollTop = el.scrollHeight;
    }
    // Overflow is only measurable while windowed (expanded drops the max-h, so
    // scrollHeight == clientHeight). Keep the last windowed verdict otherwise so
    // the "réduire" toggle stays available after expanding.
    if (!expanded) {
      setOverflowing(el.scrollHeight > el.clientHeight + 1);
    }
  }, [itemCount, lastText, expanded]);

  const finished = finalState === "done" || finalState === "failed";
  const hasTimeline = !!timeline && timeline.length > 0;

  // ACTIVE block with nothing to show yet, and not finished → render nothing.
  if (!finished && !hasTimeline) return null;

  // ── COLLAPSED summary (issue 0074) ───────────────────────────────────────
  // Once finished, the block is a compact summary by default; the timeline is
  // only re-shown when the user expands it ("relire la réflexion").
  if (finished) {
    const badge = FINAL_BADGE[finalState as "done" | "failed"];
    return (
      <div className="mt-1 rounded border border-blue-900/40 bg-blue-950/20 text-[11px]">
        <div className="flex items-center gap-2 px-2 py-1">
          <span
            className={`inline-flex flex-none items-center rounded border px-1.5 py-0.5 text-[10px] ${badge.className}`}
          >
            {badge.label}
          </span>
          {title && <span className="min-w-0 flex-1 truncate text-blue-300/80">{title}</span>}
          {hasResult && onOpenResult && (
            <button
              type="button"
              onClick={onOpenResult}
              className="flex-none rounded border border-blue-800/50 px-1.5 py-0.5 text-[10px] text-blue-300/90 transition-colors hover:bg-blue-900/30 hover:text-blue-200"
            >
              résultat
            </button>
          )}
        </div>
        {/* Expand affordance — re-shows the FULL reasoning + chips timeline
            (same full rendering as 0075's "voir tout"). Hidden when the agent
            never streamed a timeline (nothing to re-read). */}
        {hasTimeline && (
          <>
            {expanded && (
              <div
                ref={scrollRef}
                onScroll={onScroll}
                className="overflow-y-auto border-blue-900/40 border-t px-2 py-1 leading-snug text-blue-300/80"
              >
                {timeline.map(renderTimelineItem)}
              </div>
            )}
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="w-full border-blue-900/40 border-t px-2 py-0.5 text-left text-[10px] text-blue-400/70 transition-colors hover:text-blue-300"
            >
              {expanded ? "réduire" : "relire la réflexion"}
            </button>
          </>
        )}
      </div>
    );
  }

  // ── ACTIVE block (issues 0069 / 0071 / 0075) ─────────────────────────────
  return (
    <div className="mt-1 rounded border border-blue-900/40 bg-blue-950/20">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className={`overflow-y-auto px-2 py-1 text-[11px] leading-snug text-blue-300/80 ${
          expanded ? "" : "max-h-32"
        }`}
      >
        {timeline.map(renderTimelineItem)}
      </div>
      {/* Only offer the toggle when the windowed content overflows (or is
          already expanded). A short answer that fits needs no "voir tout". */}
      {(overflowing || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="w-full border-blue-900/40 border-t px-2 py-0.5 text-left text-[10px] text-blue-400/70 transition-colors hover:text-blue-300"
        >
          {expanded ? "réduire" : "voir tout"}
        </button>
      )}
    </div>
  );
}

/** Render one interleaved timeline item — a reasoning text run or an activity
 * chip. Shared by the ACTIVE window and the COLLAPSED expand view so both show
 * the identical chronological flow. The index is a stable identity here: the
 * timeline is strictly append-only — a reasoning segment's text only grows in
 * place, and chips are never removed or reordered. */
function renderTimelineItem(item: AgentTimelineItem, i: number) {
  if (item.kind === "reasoning") {
    return (
      <span key={`r-${i}`} className="whitespace-pre-wrap">
        {item.text}
      </span>
    );
  }
  const style = STATUS_STYLE[item.status];
  return (
    <span
      key={`c-${i}`}
      className={`mx-0.5 my-px inline-flex items-center gap-1 rounded border px-1.5 py-0.5 align-middle text-[10px] ${style.className}`}
      title={item.activityKind}
    >
      <span aria-hidden>{style.glyph}</span>
      <span>{item.label}</span>
    </span>
  );
}
