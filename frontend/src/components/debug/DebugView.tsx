import {
  type CSSProperties,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useDebugWs } from "../../hooks/useDebugWs";
import { useGroupedEvents } from "../../hooks/useGroupedEvents";
import { pruneEmptyNodes } from "../../lib/debugFilter";
import {
  DEBUG_CATEGORIES,
  type DebugCategory,
  type DebugFilters,
  type DebugSeverity,
} from "../../types/ws-debug";
import { DebugToolbar } from "./DebugToolbar";
import { DebugTree } from "./DebugTree";

/** Auto-clear delay for the per-`turn_id` highlight, in milliseconds. */
const TURN_HIGHLIGHT_TTL_MS = 5000;

/**
 * Pixel tolerance when deciding "the user is at the bottom". Anything within
 * this many pixels of the true bottom counts as bottom — picked to absorb
 * sub-pixel rendering rounding (which would otherwise flip `isAtBottom`
 * spuriously on certain DPRs) without letting visible scroll-up go undetected.
 */
const AT_BOTTOM_TOLERANCE_PX = 6;

/**
 * Debug window root. Renders a filter toolbar at the top and a scrollable
 * monospace feed below it. Filter state (active categories + severity
 * threshold) lives in this component rather than in `useDebugWs` because the
 * hook's mission is socket lifecycle / buffering — keeping UI-only state out
 * of it preserves a focused contract and lets future consumers subscribe to
 * the raw firehose without inheriting toolbar concerns.
 *
 * Tail-style scroll (slice 0042) lives here too: a `scrollTop`-driven
 * `isAtBottom` flag pauses auto-scroll when the user scrolls up, and a
 * floating "↓ N nouveaux events" badge surfaces unseen activity. The badge
 * resets whenever the user returns to the bottom (whether by click or by
 * manual scroll).
 *
 * Row rendering (click-to-expand + per-`turn_id` color chip) is delegated to
 * `DebugRow`. The "currently highlighted turn_id" lives here (one source of
 * truth for the whole feed) and propagates down to every row so they can
 * render the highlighted variant. Auto-clear is a single shared timeout —
 * resetting when a new turn is clicked.
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0042-debug-view-tail-scroll.md
 */
export function DebugView() {
  const { events, paused, setPaused, clear, pendingCount } = useDebugWs();
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [filters, setFilters] = useState<DebugFilters>(() => ({
    categoriesOn: new Set<DebugCategory>(DEBUG_CATEGORIES),
    severityThreshold: "info",
  }));

  const [highlightedTurnId, setHighlightedTurnId] = useState<string | null>(null);

  // Tail-scroll state. `isAtBottom` mirrors the user's scroll position
  // (`true` until they scroll up by more than `AT_BOTTOM_TOLERANCE_PX`).
  // `newEventsSinceScroll` counts visible events that landed while the user
  // was scrolled up — drives the floating badge and resets the moment they
  // return to the bottom (whether by click or by manual scroll).
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [newEventsSinceScroll, setNewEventsSinceScroll] = useState(0);

  // Mirror `isAtBottom` into a ref so the append-side effect can read the
  // current value without re-firing whenever the user's scroll position
  // changes — the effect's *trigger* is the new event landing, not a scroll.
  const isAtBottomRef = useRef(true);

  const onToggleCategory = useCallback((category: DebugCategory) => {
    setFilters((prev) => {
      const next = new Set(prev.categoriesOn);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return { ...prev, categoriesOn: next };
    });
  }, []);

  const onChangeSeverity = useCallback((severity: DebugSeverity) => {
    setFilters((prev) => ({ ...prev, severityThreshold: severity }));
  }, []);

  const onTurnClick = useCallback((turnId: string) => {
    setHighlightedTurnId(turnId);
  }, []);

  const onTogglePause = useCallback(() => {
    setPaused((p) => !p);
  }, [setPaused]);

  // Single shared 5s auto-clear timer. Re-arms whenever the highlighted
  // turn_id changes (including from one chip to another mid-flight). Cleared
  // on unmount or before the next arm fires.
  useEffect(() => {
    if (highlightedTurnId === null) return;
    const handle = window.setTimeout(() => {
      setHighlightedTurnId(null);
    }, TURN_HIGHLIGHT_TTL_MS);
    return () => {
      window.clearTimeout(handle);
    };
  }, [highlightedTurnId]);

  // Slice 0044: grouped tree replaces the linear render. We memoize the raw
  // tree on `events` identity (`useGroupedEvents`) and apply the filter as a
  // pruning pass on the tree so empty turn/task subtrees vanish but the
  // surrounding structure is preserved.
  const tree = useGroupedEvents(events);
  const prunedTree = useMemo(() => pruneEmptyNodes(tree, filters), [tree, filters]);
  // Visible event count — sum of `eventCount` across root nodes (turns/tasks
  // already aggregate their descendants; lone EventNodes count as 1 each;
  // LlmCallNodes count as 1).
  const filteredCount = useMemo(() => {
    let n = 0;
    for (const node of prunedTree) {
      switch (node.kind) {
        case "turn":
        case "task":
          n += node.eventCount;
          break;
        case "llm":
        case "event":
          n += 1;
          break;
      }
    }
    return n;
  }, [prunedTree]);

  // Scroll handler. Recomputes `isAtBottom` on every scroll tick. When the
  // user transitions back to the bottom manually (without clicking the
  // badge), we proactively reset `newEventsSinceScroll` so the badge
  // disappears as soon as they've caught up.
  const onScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance <= AT_BOTTOM_TOLERANCE_PX;
    if (atBottom !== isAtBottomRef.current) {
      isAtBottomRef.current = atBottom;
      setIsAtBottom(atBottom);
      if (atBottom) {
        setNewEventsSinceScroll(0);
      }
    }
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    isAtBottomRef.current = true;
    setIsAtBottom(true);
    setNewEventsSinceScroll(0);
  }, []);

  // Append-side autoscroll. Driven by `filteredCount` (not `events.length`)
  // so toggling a filter that removes the latest row doesn't try to scroll
  // past the truncated end. The dep on `filteredCount` is the *trigger*, not
  // consumed inside the body for branching — biome can't see the read, so we
  // mark the intent explicitly.
  //
  // Critically, this fires only on the count *changing*. Toggling expand on a
  // row or flipping a filter chip doesn't tick `filteredCount`, so expanding
  // a row that grows downward won't snap the feed to the bottom — matching
  // the PRD's "expand click does NOT trigger auto-scroll" rule.
  // biome-ignore lint/correctness/useExhaustiveDependencies: filteredCount is the autoscroll trigger
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    if (isAtBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    } else {
      setNewEventsSinceScroll((n) => n + 1);
    }
  }, [filteredCount]);

  // Space-to-toggle-pause keybind. Installed once on `document` for the
  // lifetime of the debug window. Ignored when focus is on an input-ish
  // element so it doesn't fight typing (no such elements today, but cheap
  // robustness against future filter inputs / search boxes).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.code !== "Space" && e.key !== " ") return;
      const target = e.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable) {
          return;
        }
      }
      e.preventDefault();
      setPaused((p) => !p);
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
    };
  }, [setPaused]);

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        background: "var(--bg, #02060e)",
        color: "var(--ink, #dfefff)",
        fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      }}
    >
      <DebugToolbar
        filters={filters}
        onToggleCategory={onToggleCategory}
        onChangeSeverity={onChangeSeverity}
        paused={paused}
        onTogglePause={onTogglePause}
        onClear={clear}
        visibleCount={filteredCount}
        pendingCount={pendingCount}
      />
      <div style={feedWrapperStyle}>
        <div ref={containerRef} onScroll={onScroll} style={feedScrollStyle}>
          {prunedTree.length === 0 ? (
            <div style={{ opacity: 0.45 }}>
              {events.length === 0
                ? "En attente d'événements…"
                : "Aucun événement ne correspond aux filtres actifs."}
            </div>
          ) : (
            <DebugTree
              nodes={prunedTree}
              highlightedTurnId={highlightedTurnId}
              onTurnClick={onTurnClick}
            />
          )}
        </div>
        {!isAtBottom && newEventsSinceScroll > 0 ? (
          <button
            type="button"
            onClick={scrollToBottom}
            aria-label={`Scroll to ${newEventsSinceScroll} new events`}
            style={newEventsBadgeStyle}
          >
            ↓ {newEventsSinceScroll} nouveau{newEventsSinceScroll > 1 ? "x" : ""} event
            {newEventsSinceScroll > 1 ? "s" : ""}
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Outer wrapper around the scroll container. Stays `position: relative` so
 * the floating "new events" badge can anchor to the bottom edge of the
 * visible feed area regardless of how far the inner content scrolls.
 */
const feedWrapperStyle: CSSProperties = {
  position: "relative",
  flex: 1,
  minHeight: 0,
};

/**
 * Inner scrollable feed. Padding kept compact so each row stays in the
 * 22-26px target band (`DebugRow` adds its own ~2-3px vertical padding).
 */
const feedScrollStyle: CSSProperties = {
  position: "absolute",
  inset: 0,
  overflowY: "auto",
  fontSize: "12px",
  lineHeight: "1.45",
  padding: "10px 16px",
  boxSizing: "border-box",
};

/**
 * Floating "scroll back to bottom" badge. Pinned ~14px above the bottom edge
 * of the feed wrapper so it stays visible while scrolled up. Clicking it
 * snaps to the bottom and resets the unseen-events counter.
 */
const newEventsBadgeStyle: CSSProperties = {
  position: "absolute",
  bottom: "14px",
  left: "50%",
  transform: "translateX(-50%)",
  padding: "6px 14px",
  borderRadius: "999px",
  border: "1px solid rgba(125, 211, 252, 0.55)",
  background: "rgba(2, 6, 14, 0.92)",
  color: "#dbeafe",
  fontFamily: '"JetBrains Mono", ui-monospace, monospace',
  fontSize: "11px",
  fontWeight: 600,
  letterSpacing: "0.04em",
  cursor: "pointer",
  boxShadow: "0 4px 18px rgba(0, 0, 0, 0.45)",
};
