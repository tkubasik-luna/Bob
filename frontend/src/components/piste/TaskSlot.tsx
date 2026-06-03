// TaskSlot.tsx — Piste 3D · Nacre left slot: the 3D stacked THREAD DECK
// (PRD 0014 / issues 0083, 0085, 0086).
//
// The left slot of the 3D stage (tilted +24° into depth by `.layout-depth
// .slot-task`). Rendered inside `<div className="slot-task">` by SphereUI, so
// the depth positioning already applies — this component owns only the slot
// CONTENTS and takes no props from the shell.
//
// Issue 0085 filled it with the single BOB card. Issue 0086 turns it into the
// full deck: the BOB card (front-capable) plus one `<SubCard/>` per sub-task Bob
// summoned, stacked behind it. The pure `threadDeck` model (`lib/threadDeck.ts`)
// ranks every card → transform (translate / scale / rotateZ jitter, opacity,
// z-index), picks the FRONT card (the live one, or a temporally-pinned one), and
// exposes a stable DOM order so reshuffles only GLIDE the transform — the DOM
// elements never reorder. Clicking a back card promotes it to the front (a
// temporal pin); the living card otherwise slides to the front on its own.
//
// Faithful to the mockup `ThreadStack` / `DeckCard` (`Design Mockup/p3d-panels
// .jsx`) and the deck CSS in `Design Mockup/p3d.css` (ported to `TaskSlot.css`).
//
// Idle: when there is no thread at all (Bob has no content AND no sub-tasks),
// the deck renders nothing, so the idle scene stays empty exactly like the
// mockup (sibling issue 0091 fades the panels in on first thread).

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BOB_CARD_ID,
  type DeckCard,
  type DeckCardInput,
  PIN_HOLD_MS,
  type Pin,
  activePinId,
  threadDeck,
} from "../../lib/threadDeck";
import { useActivityFeedStore } from "../../store/activityFeedStore";
import { useChatStore } from "../../store/chatStore";
import type { Task } from "../../types/ws";
import { BobCard, JARVIS_REF, bobDeckSignal, deriveBobThread } from "./BobCard";
import { SubCard } from "./SubCard";
import "./TaskSlot.css";

/** A sub-task is LIVE (eligible to auto-front) until it reaches a terminal
 * state. A settled card never auto-fronts over a working one. */
function subIsLive(state: Task["state"]): boolean {
  return state !== "done" && state !== "failed";
}

/** A sub-task's monotonic activity key = the epoch-ms of its most recent state
 * change (`updatedAt`, else `createdAt`). Higher = more recently active, so the
 * sub-task that just changed state surfaces at the front. `Date.parse` returns
 * `NaN` on a malformed/absent stamp → fall back to 0 so it never poisons the
 * sort (a card with no usable stamp simply sinks). */
function subActivity(task: Task): number {
  const stamp = task.updatedAt ?? task.createdAt;
  const ms = stamp ? Date.parse(stamp) : Number.NaN;
  return Number.isNaN(ms) ? 0 : ms;
}

export function TaskSlot() {
  // ── Store slices (the same the BobCard renders + the right-rail feed read) ─
  const messages = useChatStore((s) => s.messages);
  const streamingAssistant = useChatStore((s) => s.streamingAssistant);
  const tasksMap = useChatStore((s) => s.tasks);
  const waiting = useChatStore((s) => s.isWaitingResponse);
  const jarvisTimeline = useActivityFeedStore((s) => s.timelineByAgent[JARVIS_REF]);
  const jarvisAnswer = useActivityFeedStore((s) => s.answerByAgent[JARVIS_REF]);

  // Bob's thread, derived from the SAME pure function the card renders from
  // (no drift): we only need its phase (→ deck signal) and the idle gate here.
  const bobThread = deriveBobThread({
    messages,
    streamingAssistant,
    tasks: Object.values(tasksMap),
    waiting,
    timeline: jarvisTimeline,
    settledAnswer: jarvisAnswer,
  });
  // `bobThread.tasks` is already sorted into spawn order (the declared order).
  const subs = bobThread.tasks;

  // ── Pin (click-to-promote with temporal hold) ──────────────────────────────
  // A pin forces a card to the front for `PIN_HOLD_MS`, after which the deck
  // resumes auto-fronting the live card. We store the pin and schedule a single
  // timer to clear it at expiry so the deck re-renders and glides back.
  const [pin, setPin] = useState<Pin | null>(null);
  const pinTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const promote = useCallback((id: string) => {
    if (pinTimer.current) clearTimeout(pinTimer.current);
    setPin({ id, at: Date.now() });
    pinTimer.current = setTimeout(() => setPin(null), PIN_HOLD_MS);
  }, []);
  useEffect(() => () => void (pinTimer.current && clearTimeout(pinTimer.current)), []);

  // ── Build the deck inputs ──────────────────────────────────────────────────
  const bobSignal = bobDeckSignal(bobThread.phase);
  const bobInput: DeckCardInput = {
    kind: "bob",
    id: BOB_CARD_ID,
    live: bobSignal.live,
    activity: bobSignal.activity,
  };
  const subInputs: DeckCardInput[] = subs.map((t) => ({
    kind: "sub",
    id: t.id,
    live: subIsLive(t.state),
    activity: subActivity(t),
  }));

  // Resolve the active pin against the CURRENT card set + now, then rank.
  const cardIds = new Set<string>([BOB_CARD_ID, ...subs.map((t) => t.id)]);
  const front = activePinId(pin, Date.now(), cardIds) ?? undefined;
  const deck = threadDeck(bobInput, subInputs, front);

  // ── Idle gate ───────────────────────────────────────────────────────────────
  // No thread at all → render nothing (idle scene stays empty, per the mockup).
  if (!bobThread.hasContent && subs.length === 0) return null;

  // Map task id → Task for the SubCard render (DOM order is stable from the deck).
  const taskById = new Map(subs.map((t) => [t.id, t] as const));

  return (
    <div className="task-stack">
      {deck.domOrder.map((card) => {
        const task = card.kind === "sub" ? taskById.get(card.id) : undefined;
        // The promote affordance names the actual card (mockup `DeckCard` title).
        const label = card.kind === "bob" ? "BOB" : (task?.title ?? "la sous-tâche");
        // A card is PINNED when it is the front BECAUSE the user pinned it (not
        // merely auto-fronted) — only then does it get the pin accent + tag.
        const pinned = front !== undefined && front === card.id;
        return (
          <DeckCardShell key={card.id} card={card} promote={promote} pinned={pinned} label={label}>
            {card.kind === "bob" ? (
              <BobCard front={card.rank === 0} behind={card.behind} pinned={pinned} inDeck />
            ) : task ? (
              <SubCard task={task} front={card.rank === 0} pinned={pinned} />
            ) : null}
          </DeckCardShell>
        );
      })}
    </div>
  );
}

/**
 * One positioned card in the pile. Owns the `.stack-card` wrapper, the inline
 * rank transform (from the pure model) and — for a back card — the click that
 * promotes it to the front. The card BODY (`<BobCard/>` / `<SubCard/>`) is the
 * child, so the deck frame is decoupled from what each card renders.
 *
 * `pinned` is true only when this card is the front BECAUSE the user pinned it
 * (so only a deliberately-pinned front card gets the `.is-pinned` accent); an
 * auto-fronted card is `is-front` without the pin chrome.
 */
function DeckCardShell({
  card,
  promote,
  pinned,
  label,
  children,
}: {
  card: DeckCard;
  promote: (id: string) => void;
  pinned: boolean;
  label: string;
  children: React.ReactNode;
}) {
  const front = card.rank === 0;
  const kindClass = card.kind === "bob" ? "is-bob" : "is-sub";
  const stateClass = front ? "is-front" : "is-back";
  const cls = `stack-card ${kindClass} ${stateClass} ${pinned ? "is-pinned" : ""}`.trim();

  // The FRONT card is inert (already foregrounded); a BACK card is a
  // click/keyboard affordance that promotes it to the front (a temporal pin).
  const style: React.CSSProperties = {
    transform: card.transform.transform,
    zIndex: card.transform.zIndex,
    opacity: card.transform.opacity,
  };
  if (front) {
    return (
      <div className={cls} style={style}>
        {children}
      </div>
    );
  }
  return (
    <div
      className={cls}
      style={style}
      onClick={() => promote(card.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          promote(card.id);
        }
      }}
      // biome-ignore lint/a11y/useSemanticElements: the card is the styled mockup chrome (a `.stack-card` panel with rank transform / pin overlay), not a <button>; the role only adds button semantics while Enter/Space are wired via onKeyDown above.
      role="button"
      tabIndex={0}
      title={`Mettre « ${label} » au premier plan`}
    >
      {children}
    </div>
  );
}
