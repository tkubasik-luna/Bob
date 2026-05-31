import { useActivityFeedStore } from "../store/activityFeedStore";

type Props = {
  /** The running sub-task's id — matches the `agent_ref` on `reasoning_delta`
   * events. */
  agentRef: string;
};

/**
 * PRD 0011 / issue 0069 — minimal agent-activity block (tracer bullet).
 *
 * Renders the live streaming reasoning (chain-of-thought) of a running
 * sub-task, read from `activityFeedStore.reasoningByAgent[agentRef]` and
 * accumulated token-by-token from `reasoning_delta` WS events.
 *
 * Deliberately small and unobtrusive: it only needs to be VISIBLE somewhere in
 * the HUD during a running sub-task. The panel layout, multi-agent lanes, the
 * Jarvis block and lifecycle/retention are later issues (0070+) — this slice
 * just proves the channel cuts end-to-end. Renders nothing until the first
 * reasoning delta arrives (e.g. a model with no reasoning channel — degraded
 * mode — never shows a block here).
 */
export function AgentBlock({ agentRef }: Props) {
  const reasoning = useActivityFeedStore((s) => s.reasoningByAgent[agentRef]);
  if (!reasoning) return null;
  return (
    <div className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap rounded border border-blue-900/40 bg-blue-950/20 px-2 py-1 text-[11px] leading-snug text-blue-300/80">
      {reasoning}
    </div>
  );
}
