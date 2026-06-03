import { describe, expect, it } from "vitest";
import {
  BOB_CARD_ID,
  type DeckCardInput,
  PIN_HOLD_MS,
  type Pin,
  activePinId,
  jitterFor,
  threadDeck,
} from "./threadDeck";

// ── builders ─────────────────────────────────────────────────────────────
const bob = (over: Partial<DeckCardInput> = {}): DeckCardInput => ({
  kind: "bob",
  id: BOB_CARD_ID,
  live: true,
  activity: 0,
  ...over,
});
const sub = (id: string, over: Partial<DeckCardInput> = {}): DeckCardInput => ({
  kind: "sub",
  id,
  live: true,
  activity: 0,
  ...over,
});

describe("threadDeck", () => {
  // ── bob alone → no deck spread ────────────────────────────────────────────
  describe("bob alone (no sub-tasks)", () => {
    it("ranks a single front card with no spread and no cards behind", () => {
      const deck = threadDeck(bob(), []);
      expect(deck.ranked).toHaveLength(1);
      expect(deck.frontId).toBe(BOB_CARD_ID);
      expect(deck.behindFront).toBe(0);
      const front = deck.ranked[0];
      expect(front.rank).toBe(0);
      expect(front.behind).toBe(0);
      // front card: identity-ish transform — no translate, no rotate, full scale
      expect(front.transform.transform).toBe("translate3d(0px, 0px, 0px) rotateZ(0deg) scale(1)");
      expect(front.transform.opacity).toBe(1);
    });

    it("domOrder mirrors the single ranked card", () => {
      const deck = threadDeck(bob(), []);
      expect(deck.domOrder.map((c) => c.id)).toEqual([BOB_CARD_ID]);
    });
  });

  // ── bob + N subs → stacked deck ───────────────────────────────────────────
  describe("bob + N sub-tasks", () => {
    it("includes every card (bob + N subs) in the rank + DOM order", () => {
      const deck = threadDeck(bob(), [sub("a"), sub("b"), sub("c")]);
      expect(deck.ranked).toHaveLength(4);
      expect(deck.domOrder).toHaveLength(4);
      // DOM order is always bob then subs as declared (stable contract).
      expect(deck.domOrder.map((c) => c.id)).toEqual([BOB_CARD_ID, "a", "b", "c"]);
    });

    it("assigns increasing rank → translate/scale/z-index per the mockup math", () => {
      // Give distinct activity so the rank order is fully determined:
      // front = highest activity, then descending.
      const deck = threadDeck(bob({ activity: 30 }), [
        sub("a", { activity: 20 }),
        sub("b", { activity: 10 }),
      ]);
      const [r0, r1, r2] = deck.ranked;
      expect([r0.id, r1.id, r2.id]).toEqual([BOB_CARD_ID, "a", "b"]);
      // rank 0 (front)
      expect(r0.transform.transform).toBe("translate3d(0px, 0px, 0px) rotateZ(0deg) scale(1)");
      expect(r0.transform.zIndex).toBe(200);
      expect(r0.transform.opacity).toBe(1);
      expect(r0.behind).toBe(2);
      // rank 1 — translate (9, -50, -18), scale 0.95, jitter, z 199, opacity 0.85
      expect(r1.transform.transform).toBe(
        `translate3d(9px, -50px, -18px) rotateZ(${jitterFor("a")}deg) scale(0.95)`,
      );
      expect(r1.transform.zIndex).toBe(199);
      expect(r1.transform.opacity).toBeCloseTo(0.85, 5);
      expect(r1.behind).toBe(1);
      // rank 2 — translate (18, -100, -36), scale 0.9, z 198, opacity 0.7
      expect(r2.transform.transform).toBe(
        `translate3d(18px, -100px, -36px) rotateZ(${jitterFor("b")}deg) scale(0.9)`,
      );
      expect(r2.transform.zIndex).toBe(198);
      expect(r2.transform.opacity).toBeCloseTo(0.7, 5);
      expect(r2.behind).toBe(0);
    });

    it("floors deep-card opacity at 0.42 so the back of a big deck stays legible", () => {
      // 6 cards → rank 5 would be 1 - 5*0.15 = 0.25, floored to 0.42.
      const subs = ["a", "b", "c", "d", "e"].map((id, i) => sub(id, { activity: 10 - i }));
      const deck = threadDeck(bob({ activity: 100 }), subs);
      const deepest = deck.ranked[deck.ranked.length - 1];
      expect(deepest.rank).toBe(5);
      expect(deepest.transform.opacity).toBe(0.42);
    });

    it("reports behindFront = number of cards stacked behind the front", () => {
      expect(threadDeck(bob(), [sub("a")]).behindFront).toBe(1);
      expect(threadDeck(bob(), [sub("a"), sub("b"), sub("c")]).behindFront).toBe(3);
    });
  });

  // ── auto front selection (most-recently-active live card) ──────────────────
  describe("auto front selection", () => {
    it("fronts the live sub-task with the highest activity over bob", () => {
      const deck = threadDeck(bob({ live: true, activity: 5 }), [
        sub("a", { live: true, activity: 99 }),
        sub("b", { live: true, activity: 12 }),
      ]);
      expect(deck.frontId).toBe("a");
      expect(deck.ranked[0].id).toBe("a");
    });

    it("ignores a settled (not-live) card even if its activity is highest", () => {
      // 'a' is the most recent but DONE → must not auto-front; the live 'b' wins.
      const deck = threadDeck(bob({ live: false, activity: 1 }), [
        sub("a", { live: false, activity: 99 }),
        sub("b", { live: true, activity: 50 }),
      ]);
      expect(deck.frontId).toBe("b");
    });

    it("rests on bob when nothing is live (all sub-tasks settled)", () => {
      const deck = threadDeck(bob({ live: false, activity: 1 }), [
        sub("a", { live: false, activity: 99 }),
        sub("b", { live: false, activity: 50 }),
      ]);
      expect(deck.frontId).toBe(BOB_CARD_ID);
      expect(deck.ranked[0].id).toBe(BOB_CARD_ID);
    });

    it("breaks an activity tie deterministically toward the later-declared card", () => {
      const deck = threadDeck(bob({ live: true, activity: 10 }), [
        sub("a", { live: true, activity: 10 }),
        sub("b", { live: true, activity: 10 }),
      ]);
      // equal activity → last declared live card fronts (stable, deterministic)
      expect(deck.frontId).toBe("b");
    });
  });

  // ── promote by pin (click-to-front with temporal hold) ─────────────────────
  describe("promote by pin", () => {
    it("forces the pinned card to the front over the auto choice", () => {
      // auto would front 'a' (highest activity), but the user pinned bob.
      const deck = threadDeck(
        bob({ live: true, activity: 1 }),
        [sub("a", { activity: 99 })],
        "bob",
      );
      expect(deck.frontId).toBe(BOB_CARD_ID);
      expect(deck.ranked[0].id).toBe(BOB_CARD_ID);
      // 'a' falls to rank 1 (a back card), DOM order unchanged.
      expect(deck.ranked[1].id).toBe("a");
      expect(deck.domOrder.map((c) => c.id)).toEqual([BOB_CARD_ID, "a"]);
    });

    it("promotes a back sub-task to the front when pinned", () => {
      const deck = threadDeck(bob({ activity: 99 }), [sub("a", { activity: 1 })], "a");
      expect(deck.frontId).toBe("a");
      expect(deck.ranked[0].id).toBe("a");
    });

    it("ignores an unknown pinned id and falls back to auto", () => {
      const deck = threadDeck(bob({ activity: 99 }), [sub("a", { activity: 1 })], "ghost");
      expect(deck.frontId).toBe(BOB_CARD_ID); // auto: bob has the highest activity
    });
  });

  // ── DOM-order stability across reshuffles ──────────────────────────────────
  describe("DOM-order stability across reshuffles", () => {
    it("keeps [bob, ...subs-as-declared] regardless of which card is front", () => {
      const subs = [sub("a"), sub("b"), sub("c")];
      const expected = [BOB_CARD_ID, "a", "b", "c"];
      // front = auto (bob), front = each sub via pin → DOM order never changes.
      expect(threadDeck(bob(), subs).domOrder.map((c) => c.id)).toEqual(expected);
      expect(threadDeck(bob(), subs, "a").domOrder.map((c) => c.id)).toEqual(expected);
      expect(threadDeck(bob(), subs, "b").domOrder.map((c) => c.id)).toEqual(expected);
      expect(threadDeck(bob(), subs, "c").domOrder.map((c) => c.id)).toEqual(expected);
    });

    it("only the per-card transform/rank changes between reshuffles, never the DOM slot", () => {
      const subs = [sub("a", { activity: 1 }), sub("b", { activity: 2 })];
      const first = threadDeck(bob({ activity: 3 }), subs); // front bob
      const second = threadDeck(bob({ activity: 3 }), subs, "b"); // promote b
      // Same elements in the same DOM slots…
      expect(first.domOrder.map((c) => c.id)).toEqual(second.domOrder.map((c) => c.id));
      // …but 'b' moved from a back rank to the front between the two derives.
      const bFirst = first.domOrder.find((c) => c.id === "b");
      const bSecond = second.domOrder.find((c) => c.id === "b");
      expect(bFirst?.rank).not.toBe(0);
      expect(bSecond?.rank).toBe(0);
    });

    it("is deterministic: identical inputs produce identical output", () => {
      const mk = () => threadDeck(bob({ activity: 5 }), [sub("a", { activity: 9 }), sub("b")]);
      expect(mk()).toEqual(mk());
    });
  });

  // ── jitter is a stable pure hash of the id ─────────────────────────────────
  describe("jitterFor", () => {
    it("is deterministic and within ±4.5°", () => {
      for (const id of ["a", "courriel", "agenda", "budget", BOB_CARD_ID]) {
        const v = jitterFor(id);
        expect(v).toBe(jitterFor(id)); // stable
        expect(v).toBeGreaterThanOrEqual(-4.5);
        expect(v).toBeLessThanOrEqual(4.5);
      }
    });
  });
});

// ── temporal pin resolution ──────────────────────────────────────────────
describe("activePinId", () => {
  const ids = new Set(["bob", "a", "b"]);

  it("returns the pinned id while within the hold window", () => {
    const pin: Pin = { id: "a", at: 1000 };
    expect(activePinId(pin, 1000, ids)).toBe("a");
    expect(activePinId(pin, 1000 + PIN_HOLD_MS - 1, ids)).toBe("a");
  });

  it("expires the pin once the hold elapses", () => {
    const pin: Pin = { id: "a", at: 1000 };
    expect(activePinId(pin, 1000 + PIN_HOLD_MS, ids)).toBeNull();
    expect(activePinId(pin, 1000 + PIN_HOLD_MS + 5000, ids)).toBeNull();
  });

  it("returns null for no pin", () => {
    expect(activePinId(null, 5000, ids)).toBeNull();
    expect(activePinId(undefined, 5000, ids)).toBeNull();
  });

  it("returns null when the pinned card has left the deck", () => {
    const pin: Pin = { id: "gone", at: 1000 };
    expect(activePinId(pin, 1000, ids)).toBeNull();
  });
});
