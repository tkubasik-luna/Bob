import { useActivityFeedStore } from "../store/activityFeedStore";
import { useChatStore } from "../store/chatStore";
import type { ComponentDescriptor } from "../types/ws";
import { AgentBlock } from "./AgentBlock";

type Props = {
  /** PRD 0011 / issue 0076 — wired by `AgentActivityPanel` to open the EXISTING
   * `SectionsOverlay` for a given agent's task. Each lane resolves the task from
   * `chatStore` (the `agent_ref` is the sub-task id) and hands its
   * `result_payload` to this callback. When omitted, the result button is hidden
   * (e.g. the bare lanes view with no overlay host). */
  onOpenResult?: (sections: ComponentDescriptor[]) => void;
};

/**
 * PRD 0011 / issue 0073 — multi-agent lanes container.
 *
 * The store keys timelines by `agent_ref`, so lanes are conceptually distinct;
 * `agentOrder` enumerates the active agents in first-seen order. This container
 * simply STACKS one `AgentBlock` per agent — Jarvis plus each concurrent
 * sub-task — so several agents streaming at once each get their own block with
 * no cross-bleed (the store guarantees per-`agent_ref` isolation).
 *
 * Issue 0076 — when mounted inside the `AgentActivityPanel`, each lane is wired
 * to the EXISTING `SectionsOverlay` dispatcher: the lane looks its task up in
 * `chatStore` by `agent_ref` (= the sub-task id) and the block's "résultat"
 * button hands that task's `resultPayload` to `onOpenResult`. The Jarvis lane
 * (`agent_ref="jarvis"`) has no matching chatStore task, so it renders its
 * timeline only (no result button) — exactly what we want.
 */
export function AgentLanes({ onOpenResult }: Props) {
  const agentOrder = useActivityFeedStore((s) => s.agentOrder);
  if (agentOrder.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      {agentOrder.map((agentRef) => (
        <AgentLane key={agentRef} agentRef={agentRef} onOpenResult={onOpenResult} />
      ))}
    </div>
  );
}

/** One lane: resolves the agent's task (if any) from `chatStore` so the
 * collapsed-summary block (0074) can show the real title + wire its "résultat"
 * button to the `SectionsOverlay` dispatcher. */
function AgentLane({
  agentRef,
  onOpenResult,
}: {
  agentRef: string;
  onOpenResult?: (sections: ComponentDescriptor[]) => void;
}) {
  const task = useChatStore((s) => s.tasks[agentRef]);
  const sections = task?.resultPayload;
  const hasResult = Array.isArray(sections) && sections.length > 0;
  return (
    <AgentBlock
      agentRef={agentRef}
      title={task?.title}
      hasResult={hasResult}
      onOpenResult={
        hasResult && onOpenResult && sections ? () => onOpenResult(sections) : undefined
      }
    />
  );
}
