import { useEffect, useRef, useState } from "react";
import {
  computeActiveAgents,
  computeActivitySignal,
  shouldAutoExpand,
} from "../lib/agentActivityPanel";
import { useActivityFeedStore } from "../store/activityFeedStore";
import type { ComponentDescriptor } from "../types/ws";
import { AgentLanes } from "./AgentLanes";

type Props = {
  /** PRD 0011 / issue 0076 — open the EXISTING `SectionsOverlay` for a finished
   * agent's task. Wired by `SphereUI` to the very same `setOverlaySections`
   * dispatcher the streamed-ui / task-result paths use; the panel never
   * reimplements an overlay. The lanes resolve each task's `result_payload` and
   * pass it through here. */
  onOpenResult: (sections: ComponentDescriptor[]) => void;
};

/**
 * PRD 0011 / issue 0076 — right-edge, full-height agent-activity panel.
 *
 * Replaces `HudTasks` in the Sphere HUD. Two states:
 *   - COLLAPSED: a narrow vertical rail showing one badge per ACTIVE agent plus
 *     the active count, so the user always sees that work is happening without
 *     the panel stealing the sphere's space.
 *   - EXPANDED: a fixed right column hosting `AgentLanes` — every agent's block,
 *     active ones streaming their interleaved reasoning + chips (0075 sliding
 *     window), finished ones collapsed to a summary (0074) with a "résultat"
 *     button that opens the `SectionsOverlay`. The Jarvis lane (`agent_ref=
 *     "jarvis"`) renders alongside the sub-task lanes.
 *
 * AUTO-EXPAND: the panel pops open whenever new activity arrives (a new
 * reasoning_delta / agent_activity grows a timeline) while at least one agent is
 * active — see `shouldAutoExpand`. The user can collapse it again; we latch the
 * activity signal at collapse time so we don't immediately re-expand on the next
 * frame, only on a GENUINELY new activity edge after that.
 *
 * The panel renders nothing when there's no agent activity at all, so an idle
 * HUD stays clean (sphere centred, no rail).
 */
export function AgentActivityPanel({ onOpenResult }: Props) {
  const agentOrder = useActivityFeedStore((s) => s.agentOrder);
  const finishedByAgent = useActivityFeedStore((s) => s.finishedByAgent);
  const timelineByAgent = useActivityFeedStore((s) => s.timelineByAgent);

  const activeAgents = computeActiveAgents(agentOrder, finishedByAgent);
  const activeCount = activeAgents.length;
  const activitySignal = computeActivitySignal(timelineByAgent);

  const [expanded, setExpanded] = useState(false);
  /** Last activity signal we acted on for auto-expand. A strict increase past
   * this value (with an active agent) re-opens the panel. Collapsing by the user
   * snaps this up to the current signal so the very next frame doesn't re-expand;
   * the panel waits for the next new item. */
  const seenSignalRef = useRef(0);

  useEffect(() => {
    if (
      shouldAutoExpand({
        prevSignal: seenSignalRef.current,
        nextSignal: activitySignal,
        activeCount,
      })
    ) {
      setExpanded(true);
    }
    // Always advance the high-water mark so a later collapse + new item is the
    // only thing that re-expands (never a stale signal replay).
    if (activitySignal > seenSignalRef.current) {
      seenSignalRef.current = activitySignal;
    }
  }, [activitySignal, activeCount]);

  const handleCollapse = () => {
    // Latch: suppress auto-expand until the NEXT new activity edge.
    seenSignalRef.current = activitySignal;
    setExpanded(false);
  };

  // Nothing to show at all → render nothing (idle HUD, sphere centred).
  if (agentOrder.length === 0) return null;

  if (!expanded) {
    return (
      <div className="agent-panel agent-panel-rail">
        <button
          type="button"
          className="agent-rail-toggle"
          onClick={() => setExpanded(true)}
          aria-label="Ouvrir le flux d'activité des agents"
          aria-expanded={false}
        >
          <span className="agent-rail-count">{String(activeCount).padStart(2, "0")}</span>
          <span className="agent-rail-badges">
            {activeAgents.map((ref) => (
              <span key={ref} className="agent-rail-badge" title={ref} />
            ))}
          </span>
        </button>
      </div>
    );
  }

  return (
    <div className="agent-panel agent-panel-open">
      <div className="agent-panel-head">
        <span className="agent-panel-title">AGENTS · ACTIVITÉ</span>
        <button
          type="button"
          className="agent-panel-collapse"
          onClick={handleCollapse}
          aria-label="Réduire le flux d'activité des agents"
          aria-expanded={true}
        >
          ›
        </button>
      </div>
      <div className="agent-panel-body">
        <AgentLanes onOpenResult={onOpenResult} />
      </div>
    </div>
  );
}
