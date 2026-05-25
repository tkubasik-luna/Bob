/**
 * Recursive renderer for the grouped debug tree (slice 0044 + polish 0045).
 *
 * The tree contract lives in `lib/groupEvents.ts`. This module renders one of
 * three "header + children" node variants (`TurnNode`, `TaskNode`,
 * `LlmCallNode`) and delegates leaf event rendering to `DebugRow`.
 *
 * Slice 0045 adds:
 *  - left-border 4px + header background tint per `turn_id` on `TurnNode`
 *  - controlled expand/collapse on `Turn`, `Task`, `LlmCall` nodes via
 *    `expanded: Map<string, boolean>` + `onToggle(nodeId)` passed from
 *    `DebugView` (the single owner of expand state).
 *  - `lastInnerRef` plumbed from `DebugView` and attached to the very last
 *    rendered descendant so tail-scroll can `scrollIntoView()` precisely.
 *
 * Expand keying is by stable node id: `turn:${turnId}` / `task:${taskId}` /
 * `llm:${correlationId}` — same shape as `node.id` set in `groupEvents.ts`.
 *
 * PRD: prd/0006-debug-view-grouped-tree.md — slice: issues/0045-debug-view-tree-polish.md
 */

import { type CSSProperties, type Ref, memo, useMemo } from "react";
import type {
  LlmCallNode as LlmCallNodeT,
  TaskNode as TaskNodeT,
  TreeNode,
  TurnNode as TurnNodeT,
} from "../../lib/groupEvents";
import { shortTurnId, turnBorderColor, turnHeaderTint, turnIdColor } from "../../lib/turnColor";
import type { DebugSeverity } from "../../types/ws-debug";
import { DebugRow } from "./DebugRow";
import { HighlightedJson } from "./HighlightedJson";

/** Soft cap on the rendered "first input" preview in the turn header. */
const FIRST_INPUT_TRUNC_LEN = 80;

type TreeProps = {
  nodes: TreeNode[];
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
  /** Slice 0045: controlled expand state keyed by node id. Missing key =
   *  use slice-0045 default (expanded for Turn/Task, collapsed for Llm). */
  expanded: Map<string, boolean>;
  onToggle: (nodeId: string) => void;
  /** Optional ref attached to the very last descendant rendered under the
   *  top-level call. `DebugView` uses it to `scrollIntoView()` precisely
   *  on tail-scroll. */
  lastInnerRef?: Ref<HTMLDivElement>;
};

/**
 * Default expand state when a node id is absent from the `expanded` map.
 * Turns / tasks default open; LLM calls default collapsed (their expanded
 * body is a verbose JSON dump and would crush the feed if always open).
 */
function defaultExpanded(node: TreeNode): boolean {
  switch (node.kind) {
    case "turn":
    case "task":
      return true;
    case "llm":
      return false;
    case "event":
      return true;
  }
}

function isExpanded(node: TreeNode, expanded: Map<string, boolean>): boolean {
  const explicit = expanded.get(node.id);
  return explicit ?? defaultExpanded(node);
}

export function DebugTree({
  nodes,
  highlightedTurnId,
  onTurnClick,
  expanded,
  onToggle,
  lastInnerRef,
}: TreeProps) {
  // The lastInnerRef must end up attached to the deepest, last-rendered DOM
  // node. We pass it down only to the *final* child of the current level; the
  // final child then propagates it to its own last child, recursively. Any
  // earlier child renders without a ref.
  const lastIdx = nodes.length - 1;
  return (
    <>
      {nodes.map((node, idx) => (
        <NodeRenderer
          key={node.id}
          node={node}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
          expanded={expanded}
          onToggle={onToggle}
          lastInnerRef={idx === lastIdx ? lastInnerRef : undefined}
        />
      ))}
    </>
  );
}

function NodeRenderer({
  node,
  highlightedTurnId,
  onTurnClick,
  expanded,
  onToggle,
  lastInnerRef,
}: {
  node: TreeNode;
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
  expanded: Map<string, boolean>;
  onToggle: (nodeId: string) => void;
  lastInnerRef?: Ref<HTMLDivElement>;
}) {
  switch (node.kind) {
    case "turn":
      return (
        <TurnNodeView
          node={node}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
          expanded={expanded}
          onToggle={onToggle}
          lastInnerRef={lastInnerRef}
        />
      );
    case "task":
      return (
        <TaskNodeView
          node={node}
          highlightedTurnId={highlightedTurnId}
          onTurnClick={onTurnClick}
          expanded={expanded}
          onToggle={onToggle}
          lastInnerRef={lastInnerRef}
        />
      );
    case "llm":
      return (
        <LlmCallNodeView
          node={node}
          expanded={expanded}
          onToggle={onToggle}
          lastInnerRef={lastInnerRef}
        />
      );
    case "event":
      // Wrap so we can attach a DOM ref for tail-scroll without forwardRef'ing
      // DebugRow itself.
      return (
        <div ref={lastInnerRef}>
          <DebugRow
            event={node.event}
            highlightedTurnId={highlightedTurnId}
            onTurnClick={onTurnClick}
          />
        </div>
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

/** Caret prefix shown before a turn/task title, indicating expand state. */
function caret(open: boolean): string {
  return open ? "▾" : "▸";
}

// --- TurnNode ------------------------------------------------------------

const TurnNodeView = memo(function TurnNodeView({
  node,
  highlightedTurnId,
  onTurnClick,
  expanded,
  onToggle,
  lastInnerRef,
}: {
  node: TurnNodeT;
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
  expanded: Map<string, boolean>;
  onToggle: (nodeId: string) => void;
  lastInnerRef?: Ref<HTMLDivElement>;
}) {
  const open = isExpanded(node, expanded);
  const borderColor = turnBorderColor(node.turnId);
  const tint = turnHeaderTint(node.turnId);
  const chipColor = turnIdColor(node.turnId);
  const duration = formatDuration(node.startTs, node.endTs);
  const inputPreview = node.firstInputText
    ? truncate(node.firstInputText, FIRST_INPUT_TRUNC_LEN)
    : "";

  // Border lives on the container so the whole subtree visually "belongs" to
  // the turn (header + children share the same colored gutter on the left).
  const containerStyle: CSSProperties = {
    ...turnContainerStyle,
    borderLeft: `4px solid ${borderColor}`,
  };

  return (
    <div style={containerStyle}>
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: dev-only debug view */}
      <div
        style={{
          ...headerRowStyle,
          background: tint,
          cursor: "pointer",
        }}
        onClick={() => onToggle(node.id)}
      >
        <span style={caretStyle}>{caret(open)}</span>
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
            border: `1px solid ${chipColor}`,
            background: highlightedTurnId === node.turnId ? chipColor : "transparent",
            color: highlightedTurnId === node.turnId ? "#02060e" : chipColor,
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
      {open ? (
        <div style={childrenStyle}>
          <DebugTree
            nodes={node.children}
            highlightedTurnId={highlightedTurnId}
            onTurnClick={onTurnClick}
            expanded={expanded}
            onToggle={onToggle}
            lastInnerRef={lastInnerRef}
          />
        </div>
      ) : null}
    </div>
  );
});

// --- TaskNode ------------------------------------------------------------

const TaskNodeView = memo(function TaskNodeView({
  node,
  highlightedTurnId,
  onTurnClick,
  expanded,
  onToggle,
  lastInnerRef,
}: {
  node: TaskNodeT;
  highlightedTurnId: string | null;
  onTurnClick: (turnId: string) => void;
  expanded: Map<string, boolean>;
  onToggle: (nodeId: string) => void;
  lastInnerRef?: Ref<HTMLDivElement>;
}) {
  const open = isExpanded(node, expanded);
  const duration = formatDuration(node.startTs, node.endTs);
  const label = node.title ?? node.goal ?? "";

  return (
    <div style={taskContainerStyle}>
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: dev-only debug view */}
      <div
        style={{ ...headerRowStyle, cursor: "pointer" }}
        onClick={() => onToggle(node.id)}
      >
        <span style={caretStyle}>{caret(open)}</span>
        <span style={taskChipStyle}>📋 {node.taskId.slice(0, 6)}</span>
        {label !== "" ? <span style={taskLabelStyle}>{label}</span> : null}
        <span style={countsStyle}>
          {node.eventCount} event{node.eventCount === 1 ? "" : "s"} · {node.taskCount} task
          {node.taskCount === 1 ? "" : "s"}
        </span>
        {duration !== null ? <span style={durationStyle}>{duration}</span> : null}
        <span style={statusStyle}>{statusIcon(node.maxSeverity)}</span>
      </div>
      {open ? (
        <div style={childrenStyle}>
          <DebugTree
            nodes={node.children}
            highlightedTurnId={highlightedTurnId}
            onTurnClick={onTurnClick}
            expanded={expanded}
            onToggle={onToggle}
            lastInnerRef={lastInnerRef}
          />
        </div>
      ) : null}
    </div>
  );
});

// --- LlmCallNode ---------------------------------------------------------

const LlmCallNodeView = memo(function LlmCallNodeView({
  node,
  expanded,
  onToggle,
  lastInnerRef,
}: {
  node: LlmCallNodeT;
  expanded: Map<string, boolean>;
  onToggle: (nodeId: string) => void;
  lastInnerRef?: Ref<HTMLDivElement>;
}) {
  const open = isExpanded(node, expanded);
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
    <div ref={lastInnerRef} style={llmContainerStyle}>
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: dev-only debug view */}
      <div style={llmHeaderStyle} onClick={() => onToggle(node.id)}>
        <span style={caretStyle}>{caret(open)}</span>
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
      {open ? (
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

const caretStyle: CSSProperties = {
  opacity: 0.55,
  width: "10px",
  display: "inline-block",
  textAlign: "center",
  fontSize: "10px",
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
