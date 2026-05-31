import { useActivityFeedStore } from "../store/activityFeedStore";
import { AgentBlock } from "./AgentBlock";

/**
 * PRD 0011 / issue 0073 — multi-agent lanes container.
 *
 * The store keys timelines by `agent_ref`, so lanes are conceptually distinct;
 * `agentOrder` enumerates the active agents in first-seen order. This container
 * simply STACKS one `AgentBlock` per agent — Jarvis plus each concurrent
 * sub-task — so several agents streaming at once each get their own block with
 * no cross-bleed (the store guarantees per-`agent_ref` isolation).
 *
 * Deliberately minimal: no rail / side panel (0076), no sliding window or
 * bounded height (0075), no collapse lifecycle (0074). Just the stack. Renders
 * nothing until at least one agent has a lane, so it's unobtrusive when idle.
 *
 * NOTE: this is the GENERIC lanes view. The single-task `AgentBlock` is still
 * mounted per-card inside `TaskCard` (issue 0069/0071); this container is the
 * seam for a future panel that wants the full multi-agent feed in one place.
 */
export function AgentLanes() {
  const agentOrder = useActivityFeedStore((s) => s.agentOrder);
  if (agentOrder.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      {agentOrder.map((agentRef) => (
        <AgentBlock key={agentRef} agentRef={agentRef} />
      ))}
    </div>
  );
}
