/**
 * PRD 0014 / issue 0086 — the PURE thread-deck model.
 *
 * The left slot of the Piste 3D is a stacked deck of cards: the BOB card (the
 * orchestrator's live fil) plus one card per sub-task Bob summoned. This module
 * is the deck's brain, ported from the mockup's `useThread`/`ThreadStack`
 * (`Design Mockup/p3d-content.jsx` + `p3d-panels.jsx`) but as a deterministic,
 * UI-free function so the ordering / ranking / front-selection / promotion can
 * be unit-tested without React, the WS, or the stores.
 *
 * It does ONE thing: given the bob card and the summoned sub-task cards (each
 * carrying only the abstract signals the deck needs — a stable id, whether it is
 * "live", and a monotonic activity key), plus an optionally pinned id, it
 * returns the cards RANKED for the pile:
 *   - rank 0 is the FRONT card (pinned card, else the most-recently-active);
 *   - each rank gets a transform (translate / scale / rotateZ jitter), an
 *     opacity and a z-index, exactly mirroring the mockup's `DeckCard` math;
 *   - a separate DOM-STABLE ordering (bob first, then subs in their declared
 *     order) is exposed so the renderer keeps the same elements across
 *     reshuffles and only the transform glides — never a DOM reorder.
 *
 * The component layer maps the returned ranks/transforms onto the real
 * `<BobCard/>` / `<SubCard/>` elements and wires the click→promote pin. None of
 * the React/store plumbing lives here.
 */

/** The kind of a deck card. `bob` is the single orchestrator card; `sub` is one
 * summoned sub-task. */
export type CardKind = "bob" | "sub";

/** The fixed id of the bob card in the deck (its lane key is `"jarvis"`, but the
 * deck identifies it by this constant so a sub-task can never collide). */
export const BOB_CARD_ID = "bob";

/**
 * The abstract per-card input the deck ranks. Deliberately tiny and UI-free:
 * the caller derives these signals from the real stores (see TaskSlot), the
 * deck only orders them.
 */
export type DeckCardInput = {
  kind: CardKind;
  /** Stable identity. `BOB_CARD_ID` for the bob card; the sub-task id otherwise.
   * Drives DOM keys, the pin, and front selection — must be unique. */
  id: string;
  /** Whether this card is still working (drives front-selection: a settled card
   * never auto-fronts over a live one). Bob is live until its turn settles; a
   * sub-task is live until it reaches a terminal state. */
  live: boolean;
  /**
   * Monotonic activity key — higher = more recently active. Used to pick the
   * auto-front (the most-recently-active LIVE card) and to break ties in the
   * pile order. The caller supplies a real signal: for sub-tasks the parsed
   * `updatedAt`/`createdAt` epoch ms; for bob a value that floats to the top
   * while it is the one doing work (thinking / answering) and recedes while it
   * is merely holding the fil waiting on sub-tasks. Two cards may share a key;
   * ties fall back to a stable order so the result is deterministic.
   */
  activity: number;
};

/** A ranked card the renderer draws. Carries the input id/kind plus the
 * pile placement the component applies verbatim. */
export type DeckCard = DeckCardInput & {
  /** 0 = front; increases toward the back of the pile. */
  rank: number;
  /** How many cards sit BEHIND this one (`total - 1 - rank`). The bob FRONT
   * card surfaces this as the `+N tâches` overflow badge. */
  behind: number;
  /** Inline transform/opacity/z-index for the pile, mirroring the mockup. */
  transform: DeckTransform;
};

/** The visual placement of a card at a given rank — exactly the mockup's
 * `DeckCard` style object, computed purely so it is testable. */
export type DeckTransform = {
  /** The `transform` CSS string: `translate3d(...) rotateZ(...) scale(...)`. */
  transform: string;
  /** Stacking order — front card highest. */
  zIndex: number;
  /** Front card is fully opaque; back cards fade with depth. */
  opacity: number;
};

/** The whole computed deck the renderer consumes. */
export type ThreadDeck = {
  /** Cards ranked front→back (rank 0 first). Drives nothing about DOM order —
   * use `domOrder` for that; this is the z-stack truth. */
  ranked: DeckCard[];
  /** The same cards in a STABLE DOM order: bob first, then subs in the order the
   * caller declared them. The renderer maps over THIS (stable keys) and applies
   * each card's `transform`, so reshuffles glide the transform without ever
   * reordering the DOM (no element teardown / re-mount on promote). */
  domOrder: DeckCard[];
  /** The id of the front (rank-0) card. */
  frontId: string;
  /** How many cards sit behind the front card (= the bob badge `+N` when bob is
   * front). `0` when bob is alone / nothing is stacked. */
  behindFront: number;
};

// ── transform math — ported verbatim from the mockup's `DeckCard` ──────────

/** Per-rank translate step on X (px). */
const TX_STEP = 9;
/** Per-rank translate step on Y (px, negative = up the pile). */
const TY_STEP = -50;
/** Per-rank translate step on Z (px, negative = into depth). */
const TZ_STEP = -18;
/** Per-rank scale decrement. */
const SCALE_STEP = 0.05;
/** Per-rank opacity decrement, floored so deep cards stay legible. */
const OPACITY_STEP = 0.15;
/** Opacity floor for the deepest back cards (mockup `Math.max(0.42, …)`). */
const OPACITY_FLOOR = 0.42;
/** Base z-index of the front card; each rank back drops by one. */
const Z_BASE = 200;

/**
 * Deterministic per-id jitter angle in [-4.5°, +4.5°] — the mockup's
 * `jitterFor`. A small rotation gives back cards a hand-stacked feel; it is a
 * pure hash of the id so a card keeps the SAME tilt across reshuffles (no
 * flicker). The front card is never jittered (it reads straight).
 */
export function jitterFor(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i++) {
    h = (h * 31 + id.charCodeAt(i)) & 0xffff;
  }
  return (h % 90) / 10 - 4.5; // -4.5°..+4.5°
}

/** Compute a card's pile transform at `rank` (mirrors the mockup `DeckCard`). */
function transformFor(id: string, rank: number): DeckTransform {
  const front = rank === 0;
  const tx = rank * TX_STEP;
  const ty = rank * TY_STEP;
  const tz = rank * TZ_STEP;
  const scale = 1 - rank * SCALE_STEP;
  const rot = front ? 0 : jitterFor(id);
  return {
    transform: `translate3d(${tx}px, ${ty}px, ${tz}px) rotateZ(${rot}deg) scale(${scale})`,
    zIndex: Z_BASE - rank,
    opacity: front ? 1 : Math.max(OPACITY_FLOOR, 1 - rank * OPACITY_STEP),
  };
}

// ── front selection ─────────────────────────────────────────────────────────

/**
 * Pick the auto-front card id (no pin in effect). Mirrors the mockup's
 * `frontIdAt`: the most-recently-active LIVE card is foregrounded so you watch
 * the work happen. Rules, in order:
 *   - among LIVE cards, the one with the highest `activity` wins;
 *   - ties (equal `activity`) break toward the card declared LATER in `cards`
 *     so the resolution is deterministic and stable;
 *   - if NO card is live (everything settled), bob returns to the front — the
 *     thread rests on its own card, never on a finished sub-task.
 *
 * `cards` is expected as [bob, ...subs] in declared order (see `threadDeck`).
 */
function autoFrontId(cards: readonly DeckCardInput[]): string {
  let best: DeckCardInput | undefined;
  for (const c of cards) {
    if (!c.live) continue;
    // `>=` so a later-declared card wins an exact tie (deterministic).
    if (best === undefined || c.activity >= best.activity) best = c;
  }
  if (best) return best.id;
  // Nothing live → rest on bob (the bob card is always present as cards[0]).
  return cards.length > 0 ? cards[0].id : BOB_CARD_ID;
}

// ── pin (promote-by-click with temporal hold) ────────────────────────────────

/** How long (ms) a click-promotion pins a card to the front before the deck
 * resumes auto-fronting the live card. Mirrors the mockup's `PIN_HOLD` (7s). */
export const PIN_HOLD_MS = 7000;

/** A temporal pin: the promoted card id and the epoch-ms it was set. */
export type Pin = {
  id: string;
  /** `Date.now()` when the click happened. */
  at: number;
};

/**
 * Resolve a pin to the id it currently forces to the front, or `null` once the
 * hold elapsed OR the pinned card is no longer in the deck. Pure: the caller
 * passes `now` (so tests are deterministic) and the live card ids.
 */
export function activePinId(
  pin: Pin | null | undefined,
  now: number,
  cardIds: ReadonlySet<string>,
): string | null {
  if (!pin) return null;
  if (now - pin.at >= PIN_HOLD_MS) return null;
  // A pin on a card that has since left the deck (e.g. dropped lane) is dead.
  if (!cardIds.has(pin.id)) return null;
  return pin.id;
}

// ── the deck ─────────────────────────────────────────────────────────────────

/**
 * Build the ranked thread deck.
 *
 * @param bob   the bob card input (always present — `kind: "bob"`).
 * @param subs  the summoned sub-task cards, in DECLARED order (typically spawn
 *              order). This order is preserved verbatim in `domOrder`.
 * @param frontId  the resolved front id: the active pin (see `activePinId`) when
 *              one is held, else `undefined` to auto-front the most-recently
 *              active live card. An unknown id is ignored (falls back to auto).
 *
 * Returns the ranked z-stack + the stable DOM order + the front id + the
 * behind-front count. PURE and deterministic: identical inputs → identical
 * output, no `Date.now()` / `Math.random()` / store reads inside.
 *
 * Ranking:
 *   - the front card is rank 0;
 *   - the remaining cards keep a stable relative order — sorted by DESCENDING
 *     `activity` (most recently active nearest the front), ties broken by the
 *     DOM order (bob, then subs as declared) so reshuffles are deterministic
 *     and a quiet deck doesn't jitter its pile order.
 *
 * DOM order is independent of rank: always [bob, ...subs-as-declared].
 */
export function threadDeck(
  bob: DeckCardInput,
  subs: readonly DeckCardInput[],
  frontId?: string,
): ThreadDeck {
  // DOM order is the contract for stable elements: bob first, subs as declared.
  const declared: DeckCardInput[] = [bob, ...subs];

  // Resolve the front id: an explicit (pinned) id that actually exists wins;
  // otherwise auto-front the most-recently-active live card.
  const ids = new Set(declared.map((c) => c.id));
  const resolvedFront = frontId && ids.has(frontId) ? frontId : autoFrontId(declared);

  // Rank order: front card first, the rest by descending activity with a stable
  // DOM-index tiebreak (so equal-activity cards never swap on a re-derive).
  const domIndex = new Map(declared.map((c, i) => [c.id, i] as const));
  const rest = declared
    .filter((c) => c.id !== resolvedFront)
    .sort((a, b) => {
      if (b.activity !== a.activity) return b.activity - a.activity;
      // Stable tiebreak: earlier-declared card sits nearer the front.
      return (domIndex.get(a.id) ?? 0) - (domIndex.get(b.id) ?? 0);
    });
  const frontCard = declared.find((c) => c.id === resolvedFront);
  // `frontCard` is always defined (resolvedFront comes from `declared`), but
  // guard for type-safety without throwing.
  const rankOrder = frontCard ? [frontCard, ...rest] : rest;
  const total = rankOrder.length;

  const ranked: DeckCard[] = rankOrder.map((c, rank) => ({
    ...c,
    rank,
    behind: total - 1 - rank,
    transform: transformFor(c.id, rank),
  }));

  // Project the ranked placement back onto the stable DOM order so the renderer
  // can keep elements put and just apply each card's transform.
  const rankedById = new Map(ranked.map((c) => [c.id, c] as const));
  const domOrder = declared
    .map((c) => rankedById.get(c.id))
    .filter((c): c is DeckCard => c !== undefined);

  const behindFront = total > 0 ? total - 1 : 0;

  return { ranked, domOrder, frontId: resolvedFront, behindFront };
}
