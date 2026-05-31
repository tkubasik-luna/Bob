import type { AgentTimelineItem } from "../store/activityFeedStore";
import type { TaskState } from "../types/ws";

/**
 * PRD 0011 / issue 0076 — pure logic for the right-edge AgentActivityPanel.
 *
 * The panel reads three slices off `activityFeedStore`: `agentOrder` (first-seen
 * lane order), `finishedByAgent` (per-agent terminal state) and `timelineByAgent`
 * (the interleaved reasoning + chip items). This module derives, with NO React,
 * the things the panel needs:
 *   - which agents are ACTIVE (have a lane but haven't terminated) — drives the
 *     collapsed rail badges + the active count;
 *   - a monotonic ACTIVITY SIGNAL (total timeline items across all agents) — the
 *     panel watches it to AUTO-EXPAND when new reasoning / chips arrive while an
 *     agent is still running;
 *   - the auto-expand decision itself, so the "should we pop the panel open?"
 *     rule is unit-testable in isolation.
 *
 * Everything here is a pure function of store state so the panel stays a thin
 * presentational shell over `AgentLanes` + the rail.
 */

/** An agent is ACTIVE when it has a lane in `agentOrder` but has NOT recorded a
 * terminal state in `finishedByAgent`. Finished agents still render (collapsed
 * summary) but they don't count toward the live rail badges / count. */
export function computeActiveAgents(
  agentOrder: string[],
  finishedByAgent: Record<string, TaskState>,
): string[] {
  return agentOrder.filter((ref) => !(ref in finishedByAgent));
}

/** Count of active (running) agents — the number shown on the collapsed rail. */
export function computeActiveCount(
  agentOrder: string[],
  finishedByAgent: Record<string, TaskState>,
): number {
  return computeActiveAgents(agentOrder, finishedByAgent).length;
}

/**
 * A single monotonic number summarising "how much activity has accumulated":
 * the total count of timeline items across every agent. It only ever grows for
 * the lifetime of a run (items are append-only until `reset`/`clearAgent`), so a
 * strict increase is an unambiguous "new activity arrived" edge the panel can
 * latch onto for auto-expand.
 */
export function computeActivitySignal(
  timelineByAgent: Record<string, AgentTimelineItem[]>,
): number {
  let total = 0;
  for (const ref in timelineByAgent) {
    total += timelineByAgent[ref].length;
  }
  return total;
}

/**
 * Auto-expand decision. The panel pops open when BOTH:
 *   - the activity signal strictly increased since the last observation (new
 *     reasoning_delta / agent_activity landed), AND
 *   - at least one agent is currently active (running) — we don't re-expand for
 *     a late item on an already-finished agent.
 *
 * A drop or no-change in the signal (e.g. a lane was cleared) never auto-expands.
 * The user collapsing the panel sets the latch (`userCollapsed`) which suppresses
 * auto-expand until the NEXT genuinely-new activity edge — handled by the caller
 * resetting that latch when the signal advances; here we just gate on the edge +
 * active presence.
 */
export function shouldAutoExpand(args: {
  prevSignal: number;
  nextSignal: number;
  activeCount: number;
}): boolean {
  return args.nextSignal > args.prevSignal && args.activeCount > 0;
}
