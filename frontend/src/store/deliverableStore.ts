import { create } from "zustand";
import type { DeliverableCardTask } from "../lib/deliverableCard";
import type { ComponentDescriptor } from "../types/ws";

/**
 * PRD 0014 / issue 0087 — the SESSION-SCOPED deliverable store.
 *
 * Collects every deliverable Bob's session generates so the « DONNÉES GÉNÉRÉES »
 * dock (right slot) can render one card per artefact. A deliverable is the
 * `ComponentDescriptor[]` the SectionsOverlay consumes as a stack; it has two
 * sources, both ingested here (the plumbing lives in
 * `components/piste/useDeliverableIngest.ts`):
 *
 *   1. a sub-task's `task_result.result_payload` — the structured deliverable a
 *      sub-agent resolved to (`chatStore.tasks[id].resultPayload`), keyed by the
 *      task id;
 *   2. Bob's own streamed `ui_payload` — a SINGLE descriptor the orchestrator
 *      emits, wrapped here into a list-of-one, keyed by its `msg_id`.
 *
 * Design (mirrors the retention discipline of `activityFeedStore`):
 *   - SESSION-SCOPED + in-memory only. Nothing is persisted across reloads; the
 *     dock rebuilds from the connect-time `task_*` replay (see the ingest hook).
 *   - NO TTL / NO automatic eviction. Unlike the mockup's decaying memory tray,
 *     a real deliverable persists for the WHOLE session. The set only shrinks on
 *     an explicit `reset()` (a fresh session).
 *   - DEDUPE BY SOURCE ID. Re-ingesting the same id (replay, a duplicate frame)
 *     is a no-op — the first insertion wins, so a card never flickers or
 *     re-animates `fresh` on reconnect.
 *   - FRESH → SEEN. A new entry lands `fresh` (the card animates on arrival).
 *     Clicking the card opens the overlay and flips it to `seen` via
 *     `markSeen(id)`. `activeCount` is the number of still-`fresh` entries — the
 *     "compteur d'actives" the dock header shows.
 *   - INSERTION-ORDERED. A monotonic `seq` records arrival order so the dock can
 *     render newest-first deterministically without relying on object key order.
 */

/** Fresh = just arrived, not yet opened (animates, counts as "active"). Seen =
 * the user has opened its overlay at least once. */
export type DeliverableStatus = "fresh" | "seen";

/** One stored deliverable. `task` is the light projection input the dock hands
 * to `toCard`; `deliverable` is the descriptor stack the overlay renders. */
export type DeliverableEntry = {
  /** Stable source id — a sub-task `task_id` or a Bob `msg_id`. Dedupe key. */
  id: string;
  /** The descriptor stack (overlay input). */
  deliverable: ComponentDescriptor[];
  /** Projection input for `toCard` (title + optional goal). */
  task: DeliverableCardTask;
  /** Lifecycle: `fresh` until the user opens it, then `seen`. */
  status: DeliverableStatus;
  /** Monotonic arrival order (newest = highest). */
  seq: number;
};

/** What the ingest layer passes to add a deliverable. `id` dedupes; `task`
 * feeds the projection; `deliverable` is the overlay stack. */
export type AddDeliverableInput = {
  id: string;
  deliverable: ComponentDescriptor[];
  task: DeliverableCardTask;
};

type DeliverableState = {
  /** All stored deliverables keyed by source id (dedupe). Insertion order is
   * NOT relied upon — `seq` carries arrival order. */
  byId: Record<string, DeliverableEntry>;
  /** Monotonic counter handed to the next added entry. */
  nextSeq: number;
  /** Ingest a deliverable. No-op if `id` is already present (dedupe), or if the
   * descriptor list is empty (nothing to render / open). New entries land
   * `fresh`. */
  add: (input: AddDeliverableInput) => void;
  /** Flip an entry `fresh` → `seen` (called on card click). Idempotent; a no-op
   * for an unknown id or an already-`seen` entry. */
  markSeen: (id: string) => void;
  /** Wipe the store (new session). */
  reset: () => void;
};

export const useDeliverableStore = create<DeliverableState>((set) => ({
  byId: {},
  nextSeq: 0,
  add: ({ id, deliverable, task }) =>
    set((state) => {
      // Dedupe by source id — the first insertion wins so a replayed/duplicate
      // frame never re-animates the card or churns the store.
      if (state.byId[id]) return state;
      // Nothing renderable / openable → ignore (defensive; the ingest hook
      // already filters, but keep the store honest).
      if (deliverable.length === 0) return state;
      const entry: DeliverableEntry = {
        id,
        deliverable,
        task,
        status: "fresh",
        seq: state.nextSeq,
      };
      return {
        byId: { ...state.byId, [id]: entry },
        nextSeq: state.nextSeq + 1,
      };
    }),
  markSeen: (id) =>
    set((state) => {
      const entry = state.byId[id];
      // Unknown id or already seen → no churn (idempotent).
      if (!entry || entry.status === "seen") return state;
      return {
        byId: { ...state.byId, [id]: { ...entry, status: "seen" } },
      };
    }),
  reset: () => set({ byId: {}, nextSeq: 0 }),
}));

/** Selector — entries newest-first (highest `seq` first). Stable to compute
 * from `byId`; the dock memoises it so a `markSeen` only re-renders on a real
 * change. Exported for the dock + tests. */
export function selectOrdered(byId: Record<string, DeliverableEntry>): DeliverableEntry[] {
  return Object.values(byId).sort((a, b) => b.seq - a.seq);
}

/** Selector — count of still-`fresh` (unseen) deliverables. This is the
 * "artefacts actifs" figure the dock header surfaces. */
export function selectActiveCount(byId: Record<string, DeliverableEntry>): number {
  let n = 0;
  for (const id in byId) {
    if (byId[id].status === "fresh") n++;
  }
  return n;
}
