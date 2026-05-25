/**
 * Recursive renderer for the grouped debug tree (slice 0044).
 *
 * The tree contract lives in `lib/groupEvents.ts`. This module renders one of
 * three "header + children" node variants (`TurnNode`, `TaskNode`,
 * `LlmCallNode`) and delegates leaf event rendering to `DebugRow`.
 *
 * Auto-expand state for this slice = always expanded (no collapse map yet —
 * slice 0045 owns the expand/collapse UX). The `LlmCallNode` and per-row
 * JSON payload toggles remain locally controlled for now.
 *
 * Visual scaffolding is intentionally minimal — slice 0045 layers the
 * per-turn border tint, the expand toggle chevron, and the snapshot-replay
 * "collapsed-except-last" rule on top.
 *
 * PRD: prd/0006-debug-view-grouped-tree.md — slice: issues/0044-debug-view-grouped-tree.md
 */

import { type CSSProperties, memo, useMemo, useState } from "react";
import type {
  LlmCallNode as LlmCallNodeT,
  TaskNode as TaskNodeT,
  TreeNode,
  TurnNode as TurnNodeT,
} from "../../lib/groupEvents";
import { shortTurnId, turnIdColor } from "../../lib/turnColor";
import type { DebugSeverity } from "../../types/ws-debug";
import { DebugRow } from "./DebugRow";
import { HighlightedJson } from "./HighlightedJson";

/** Soft cap on the rendered "first input" preview in the turn header. */
const FIRST_INPUT_TRUNC_LEN = 80;

type TreeProps = {
  nodes: TreeNode[];
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
};

export function DebugTree({ nodes, highlightedTurnId, onTurnClick }: TreeProps) {
  return (
    <>
      {nodes.map((node) => (
        <NodeRenderer
          key={node.id}
          node={node}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
        />
      ))}
    </>
  );
}

function NodeRenderer({
  node,
  highlightedTurnId,
  onTurnClick,
}: {
  node: TreeNode;
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
}) {
  switch (node.kind) {
    case "turn":
      return (
        <TurnNodeView
          node={node}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
        />
      );
    case "task":
      return (
        <TaskNodeView
          node={node}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
        />
      );
    case "llm":
      return <LlmCallNodeView node={node} />;
    case "event":
      return (
        <DebugRow
          event={node.event}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
        />
      );
  }
}

/** Status icon driven by the highest severity in the node's subtree. */
function statusIcon(maxSeverity: DebugSeverity): string {
  switch (maxSeverity) {
    case "error":
      return "❌";
    case "warn":
      return "⚠️";
    default:
      return "✅";
  }
}

/** Wall-clock duration label, e.g. `"1.2s"` / `"340ms"`. Returns `null`
 *  when start/end aren't both present (in-flight or empty subtree). */
function formatDuration(startTs: string, endTs: string): string | null {
  if (startTs === "" || endTs === "") return null;
  const ms = Date.parse(endTs) - Date.parse(startTs);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return `${s.slice(0, max - 1)}…`;
}

// --- TurnNode ------------------------------------------------------------

const TurnNodeView = memo(function TurnNodeView({
  node,
  highlightedTurnId,
  onTurnClick,
}: {
  node: TurnNodeT;
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
}) {
  const color = turnIdColor(node.turnId);
  const duration = formatDuration(node.startTs, node.endTs);
  const inputPreview = node.firstInputText
    ? truncate(node.firstInputText, FIRST_INPUT_TRUNC_LEN)
    : "";

  return (
    <div style={turnContainerStyle}>
      <div
        style={{
          ...headerRowStyle,
          borderLeft: `3px solid ${color}`,
        }}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onTurnClick(node.turnId);
          }}
          aria-label={`Highlight turn ${shortTurnId(node.turnId)}`}
          title={`turn_id: ${node.turnId}`}
          style={{
            ...chipBaseStyle,
            border: `1px solid ${color}`,
            background: highlightedTurnId === node.turnId ? color : "transparent",
            color: highlightedTurnId === node.turnId ? "#02060e" : color,
          }}
        >
          {shortTurnId(node.turnId)}
        </button>
        <span style={inputPreviewStyle}>{inputPreview}</span>
        <span style={countsStyle}>
          {node.eventCount} event{node.eventCount === 1 ? "" : "s"} · {node.taskCount} task
          {node.taskCount === 1 ? "" : "s"}
        </span>
        {duration !== null ? <span style={durationStyle}>{duration}</span> : null}
        <span style={statusStyle}>{statusIcon(node.maxSeverity)}</span>
      </div>
      <div style={childrenStyle}>
        <DebugTree
          nodes={node.children}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
        />
      </div>
    </div>
  );
});

// --- TaskNode ------------------------------------------------------------

const TaskNodeView = memo(function TaskNodeView({
  node,
  highlightedTurnId,
  onTurnClick,
}: {
  node: TaskNodeT;
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
}) {
  const duration = formatDuration(node.startTs, node.endTs);
  const label = node.title ?? node.goal ?? "";

  return (
    <div style={taskContainerStyle}>
      <div style={headerRowStyle}>
        <span style={taskChipStyle}>📋 {node.taskId.slice(0, 6)}</span>
        {label !== "" ? <span style={taskLabelStyle}>{label}</span> : null}
        <span style={countsStyle}>
          {node.eventCount} event{node.eventCount === 1 ? "" : "s"} · {node.taskCount} task
          {node.taskCount === 1 ? "" : "s"}
        </span>
        {duration !== null ? <span style={durationStyle}>{duration}</span> : null}
        <span style={statusStyle}>{statusIcon(node.maxSeverity)}</span>
      </div>
      <div style={childrenStyle}>
        <DebugTree
          nodes={node.children}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
        />
      </div>
    </div>
  );
});

// --- LlmCallNode ---------------------------------------------------------

const LlmCallNodeView = memo(function LlmCallNodeView({ node }: { node: LlmCallNodeT }) {
  const [expanded, setExpanded] = useState(false);
  const latencyLabel =
    node.end === undefined
      ? "⏳"
      : node.latencyMs !== null
        ? `${Math.round(node.latencyMs)}ms`
        : "—";
  const tokensLabel =
    node.tokensIn !== null || node.tokensOut !== null
      ? `${node.tokensIn ?? "?"}→${node.tokensOut ?? "?"} tok`
      : null;
  const model = node.model ?? "?";

  const prettyMessages = useMemo(() => {
    const p = node.start.payload as Record<string, unknown>;
    const messages = p.messages;
    return messages !== undefined ? JSON.stringify(messages, null, 2) : null;
  }, [node.start.payload]);

  const prettyResponse = useMemo(() => {
    if (node.end === undefined) return null;
    const p = node.end.payload as Record<string, unknown>;
    return JSON.stringify(p, null, 2);
  }, [node.end]);

  return (
    <div style={llmContainerStyle}>
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: dev-only debug view */}
      <div style={llmHeaderStyle} onClick={() => setExpanded((v) => !v)}>
        <span>🧠 LLM</span>
        <span style={llmSepStyle}>·</span>
        <span style={llmModelStyle}>{model}</span>
        <span style={llmSepStyle}>·</span>
        <span style={llmLatencyStyle}>{latencyLabel}</span>
        {tokensLabel !== null ? (
          <>
            <span style={llmSepStyle}>·</span>
            <span style={llmTokensStyle}>{tokensLabel}</span>
          </>
        ) : null}
        <span style={statusStyle}>{statusIcon(node.maxSeverity)}</span>
      </div>
      {expanded ? (
        <div style={llmExpandedStyle}>
          {prettyMessages !== null ? (
            <>
              <div style={llmLabelStyle}>messages</div>
              <HighlightedJson json={prettyMessages} />
            </>
          ) : null}
          {prettyResponse !== null ? (
            <>
              <div style={llmLabelStyle}>response</div>
              <HighlightedJson json={prettyResponse} />
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});

// --- styles --------------------------------------------------------------

const turnContainerStyle: CSSProperties = {
  margin: "8px 0",
};
const taskContainerStyle: CSSProperties = {
  margin: "4px 0",
};
const llmContainerStyle: CSSProperties = {
  margin: "2px 0",
};

const headerRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "8px",
  padding: "2px 6px",
  fontSize: "12px",
};
const llmHeaderStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "6px",
  padding: "2px 6px",
  cursor: "pointer",
  fontSize: "12px",
  color: "rgba(223, 239, 255, 0.88)",
};

const childrenStyle: CSSProperties = {
  marginLeft: "16px",
  paddingLeft: "8px",
  borderLeft: "1px dashed rgba(255, 255, 255, 0.08)",
};

const chipBaseStyle: CSSProperties = {
  display: "inline-block",
  padding: "0 6px",
  borderRadius: "3px",
  fontSize: "10px",
  fontWeight: 600,
  letterSpacing: "0.04em",
  fontFamily: "inherit",
  cursor: "pointer",
};

const taskChipStyle: CSSProperties = {
  display: "inline-block",
  padding: "0 6px",
  borderRadius: "3px",
  fontSize: "10px",
  fontWeight: 600,
  letterSpacing: "0.04em",
  border: "1px solid rgba(167, 139, 250, 0.6)",
  color: "rgba(196, 181, 253, 0.95)",
  background: "rgba(76, 29, 149, 0.18)",
};

const inputPreviewStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "rgba(223, 239, 255, 0.92)",
};
const taskLabelStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "rgba(223, 239, 255, 0.85)",
};
const countsStyle: CSSProperties = {
  opacity: 0.6,
  fontSize: "11px",
  whiteSpace: "nowrap",
};
const durationStyle: CSSProperties = {
  opacity: 0.6,
  fontSize: "11px",
  whiteSpace: "nowrap",
};
const statusStyle: CSSProperties = {
  fontSize: "11px",
};

const llmSepStyle: CSSProperties = { opacity: 0.35 };
const llmModelStyle: CSSProperties = { color: "rgba(196, 181, 253, 0.9)" };
const llmLatencyStyle: CSSProperties = { color: "rgba(252, 211, 77, 0.9)" };
const llmTokensStyle: CSSProperties = { color: "rgba(190, 242, 100, 0.9)" };

const llmExpandedStyle: CSSProperties = {
  marginLeft: "16px",
  padding: "6px 10px",
  borderLeft: "1px dashed rgba(255, 255, 255, 0.08)",
  fontSize: "11px",
};
const llmLabelStyle: CSSProperties = {
  marginTop: "4px",
  opacity: 0.55,
  fontSize: "10px",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};
