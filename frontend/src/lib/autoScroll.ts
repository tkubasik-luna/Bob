/**
 * PRD 0011 / issue 0075 — pure auto-scroll / windowing helpers for the ACTIVE
 * agent block's bounded reasoning window.
 *
 * The active `AgentBlock` shows a bounded-height window of the latest reasoning
 * and auto-scrolls to the newest tokens as deltas arrive — but only while the
 * user is "stuck to the bottom". If the user scrolls up to read back, we pause
 * auto-scroll until they return to the bottom (standard chat-log behaviour).
 *
 * The decision is pure arithmetic over scroll metrics, so it lives here and is
 * unit-tested without any DOM. The component just feeds it the element's
 * `scrollTop` / `clientHeight` / `scrollHeight` and applies the result.
 */

/** Scroll metrics read off a scrollable element (a subset of `HTMLElement`). */
export type ScrollMetrics = {
  scrollTop: number;
  clientHeight: number;
  scrollHeight: number;
};

/**
 * Distance (px) from the bottom within which the viewport still counts as
 * "at the bottom". A small slack absorbs sub-pixel rounding and the last
 * line's leading so auto-scroll doesn't drop out one pixel early.
 */
export const AT_BOTTOM_THRESHOLD_PX = 24;

/** How far the content is scrolled away from the very bottom, in px. */
export function distanceFromBottom(m: ScrollMetrics): number {
  return m.scrollHeight - m.clientHeight - m.scrollTop;
}

/**
 * True when the viewport is at (or within `threshold` of) the bottom — i.e. the
 * user has NOT scrolled up to read back, so new tokens should keep it pinned.
 */
export function isAtBottom(m: ScrollMetrics, threshold = AT_BOTTOM_THRESHOLD_PX): boolean {
  return distanceFromBottom(m) <= threshold;
}

/**
 * Decide whether to auto-scroll to the bottom after new content arrived.
 *
 * `stuckToBottom` is the latched intent from the last user scroll: it stays
 * true until the user scrolls up, and flips back to true once they scroll back
 * down to the bottom. When expanded ("voir tout"), the window no longer bounds
 * height and we don't fight the user, so auto-scroll is disabled.
 */
export function shouldAutoScroll(opts: { stuckToBottom: boolean; expanded: boolean }): boolean {
  return opts.stuckToBottom && !opts.expanded;
}
