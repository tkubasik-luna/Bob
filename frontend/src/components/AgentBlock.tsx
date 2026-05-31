import { useActivityFeedStore } from "../store/activityFeedStore";
import type { AgentActivityStatus } from "../types/ws";

type Props = {
  /** The running sub-task's id — matches the `agent_ref` on `reasoning_delta`
   * and `agent_activity` events. */
  agentRef: string;
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
 * Deliberately minimal and unobtrusive. Lanes (0073), sliding window / collapse
 * (0075), the Jarvis block (0072) and the panel layout (0076) are later issues.
 * Renders nothing until the first item arrives (a model with no reasoning
 * channel still surfaces its chips here once it acts).
 */
export function AgentBlock({ agentRef }: Props) {
  const timeline = useActivityFeedStore((s) => s.timelineByAgent[agentRef]);
  if (!timeline || timeline.length === 0) return null;
  return (
    <div className="mt-1 max-h-32 overflow-y-auto rounded border border-blue-900/40 bg-blue-950/20 px-2 py-1 text-[11px] leading-snug text-blue-300/80">
      {timeline.map((item, i) => {
        if (item.kind === "reasoning") {
          return (
            // biome-ignore lint/suspicious/noArrayIndexKey: the timeline is strictly append-only — a reasoning segment's index is fixed once a chip is appended after it (its text only grows in place), and chips are never removed or reordered, so the index is a stable identity.
            <span key={`r-${i}`} className="whitespace-pre-wrap">
              {item.text}
            </span>
          );
        }
        const style = STATUS_STYLE[item.status];
        return (
          <span
            // biome-ignore lint/suspicious/noArrayIndexKey: append-only timeline (see reasoning branch) — the chip's index is its stable identity.
            key={`c-${i}`}
            className={`mx-0.5 my-px inline-flex items-center gap-1 rounded border px-1.5 py-0.5 align-middle text-[10px] ${style.className}`}
            title={item.activityKind}
          >
            <span aria-hidden>{style.glyph}</span>
            <span>{item.label}</span>
          </span>
        );
      })}
    </div>
  );
}
