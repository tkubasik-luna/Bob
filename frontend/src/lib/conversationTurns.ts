import type { AgentTimelineItem, JarvisSegment } from "../store/activityFeedStore";
import type { ChatMessage, Task } from "../types/ws";
import { type BobFlowNode, buildBobFlow, isDelegateChip } from "./threadFlow";

/**
 * PRD 0014 — split Bob's session into a CHAT TRANSCRIPT of per-turn blocks.
 *
 * The BobCard used to render only the latest turn (last prompt + last answer),
 * so each new turn visually REPLACED the previous. The user wants the
 * conversation to accumulate like a chat — every turn stacked, newest at the
 * bottom. The chat messages already accumulate (`chatStore.messages`), but the
 * Jarvis reasoning/chip timeline is ONE flat append-only lane with no turn
 * boundaries in it. The `activityFeedStore` captures those boundaries on the
 * fly: at each `thinking: start` it records the timeline index where the turn
 * began (`jarvisTurnPending`), and on the closing `assistant_msg` it binds that
 * index to the reply's `msg_id` (a committed {@link JarvisSegment}).
 *
 * This pure builder folds messages + timeline + tasks + segments back into the
 * ordered turn blocks the card renders. Keying each turn's slice by the
 * assistant `msg_id` (not by order) makes it robust to drift: an errored or
 * proactive turn that produced no segment simply renders with an empty flow
 * (just prompt + answer) instead of mis-attributing another turn's reasoning.
 *
 * Pure + exported so the segmentation is unit-testable without React / the WS.
 */

export type ConversationTurn = {
  /** Stable React key — the assistant `msg_id`, or a synthetic id for the live
   * turn / a rehydrated tasks-only block. */
  key: string;
  /** The user prompt that opened the turn ("" for a proactive synthesis or a
   * turn whose prompt isn't known). */
  prompt: string;
  /** True for a proactive Bob push (no user prompt). */
  proactive: boolean;
  /** Réflexion runs + delegated-task groups for this turn, chronological. */
  flow: BobFlowNode[];
  /** The settled (or, for the live turn, streaming) reply text. */
  answerText: string;
  /** True for the single in-flight turn — drives the live caret / active pip. */
  inFlight: boolean;
};

export type BuildConversationInput = {
  messages: readonly ChatMessage[];
  /** The flat (accumulating) Jarvis lane timeline. */
  timeline: readonly AgentTimelineItem[] | undefined;
  /** All sub-tasks, any turn (correlated to délègue chips by createdAt order). */
  tasks: readonly Task[];
  /** Committed turn boundaries, in occurrence order. */
  segments: readonly JarvisSegment[];
  /** The live turn's start index, or null between turns. */
  pending: number | null;
  /** The in-flight reply suffix (`streamingAssistant.speech`). */
  streamingSpeech: string;
};

export function buildConversation(input: BuildConversationInput): ConversationTurn[] {
  const { messages, timeline, tasks, segments, pending, streamingSpeech } = input;
  const items = timeline ?? [];
  const n = items.length;
  const clamp = (x: number) => Math.max(0, Math.min(x, n));

  // createdAt order == spawned order == délègue-chip order across the session,
  // so the k-th délègue chip (anywhere in the lane) maps to the k-th task.
  const sortedTasks = [...tasks].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  // Prefix count of délègue chips, so a slice picks exactly its own tasks.
  const delegatePrefix: number[] = new Array(n + 1);
  delegatePrefix[0] = 0;
  for (let i = 0; i < n; i++) {
    const it = items[i];
    delegatePrefix[i + 1] = delegatePrefix[i] + (it.kind === "chip" && isDelegateChip(it) ? 1 : 0);
  }
  const totalDelegates = delegatePrefix[n];

  // Build a turn's flow from its timeline slice, feeding `buildBobFlow` exactly
  // the tasks whose délègue chip falls inside the slice (in order), so its
  // local cursor correlates them correctly.
  const sliceFlow = (start: number, end: number): BobFlowNode[] => {
    const s = clamp(start);
    const e = Math.max(s, clamp(end));
    const before = delegatePrefix[s];
    const within = delegatePrefix[e] - before;
    return buildBobFlow(items.slice(s, e), sortedTasks.slice(before, before + within));
  };

  // msgId → [start, end): each segment runs to the next segment's start, or to
  // the live pending start / the timeline end for the last committed one.
  const sliceByMsgId = new Map<string, { start: number; end: number }>();
  for (let i = 0; i < segments.length; i++) {
    const end = i + 1 < segments.length ? segments[i + 1].start : (pending ?? n);
    sliceByMsgId.set(segments[i].msgId, { start: segments[i].start, end });
  }

  const turns: ConversationTurn[] = [];
  let promptBuf = "";
  for (const m of messages) {
    if (m.role === "user") {
      promptBuf = m.content;
      continue;
    }
    // An assistant message closes a turn.
    const slice = sliceByMsgId.get(m.id);
    turns.push({
      key: m.id,
      prompt: m.proactive ? "" : promptBuf,
      proactive: m.proactive === true,
      flow: slice ? sliceFlow(slice.start, slice.end) : [],
      answerText: m.content,
      inFlight: false,
    });
    promptBuf = "";
  }

  // A trailing user message with no assistant reply yet is the live turn.
  const last = messages.length > 0 ? messages[messages.length - 1] : undefined;
  if (last && last.role === "user") {
    const start = pending ?? n;
    turns.push({
      key: "live",
      prompt: last.content,
      proactive: false,
      flow: sliceFlow(start, n),
      answerText: streamingSpeech,
      inFlight: true,
    });
  }

  // Tasks with no délègue chip anywhere (rehydrate-on-reload replays tasks but
  // not the lane; or a chip not yet arrived) trail the last turn — or stand
  // alone when there is no conversation yet (a reconnect showing tasks only).
  const leftover = sortedTasks.slice(totalDelegates);
  if (leftover.length > 0) {
    if (turns.length > 0) {
      const tail = turns[turns.length - 1];
      tail.flow = [...tail.flow, { kind: "tasks", tasks: leftover }];
    } else {
      turns.push({
        key: "orphan-tasks",
        prompt: "",
        proactive: false,
        flow: [{ kind: "tasks", tasks: leftover }],
        answerText: "",
        inFlight: false,
      });
    }
  }

  return turns;
}
