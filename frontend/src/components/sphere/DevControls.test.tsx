import { fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Vitest 4 + jsdom 29 ships without a real `localStorage` implementation
// (Node's experimental impl requires `--localstorage-file`). Install a
// minimal in-memory polyfill BEFORE the store module loads — otherwise the
// store's `hydrate()` path bails inside its try/catch and the persistence
// test has nothing to read back. `vi.hoisted()` runs before any `import` in
// this file, which is the only safe spot to mutate `window` ahead of the
// store evaluation.
vi.hoisted(() => {
  if (typeof window === "undefined") return;
  // biome-ignore lint/suspicious/noExplicitAny: feature-detect on window without bringing the Storage type into hoisted scope
  const w = window as any;
  if (typeof w.localStorage !== "undefined" && w.localStorage !== null) return;
  const store = new Map<string, string>();
  const polyfill = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key: string) => (store.has(key) ? (store.get(key) ?? null) : null),
    key: (idx: number) => Array.from(store.keys())[idx] ?? null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(window, "localStorage", {
    value: polyfill,
    writable: true,
    configurable: true,
  });
});

import {
  DEV_TWEAKS_DEFAULTS,
  DEV_TWEAKS_STORAGE_KEY,
  useDevTweaksStore,
} from "../../state/devTweaksStore";
import { DevControls } from "./DevControls";

const initialStoreState = useDevTweaksStore.getState();
const originalLocation = window.location;

/** Replace `window.location` with a stub that exposes a custom `search`. The
 * native `Location` object isn't user-writable, so we `defineProperty` a
 * minimal mock for the duration of the test and restore the original after.
 * Other Location members aren't touched by `DevControls` (it only reads
 * `search`), so a partial mock is enough. */
function stubLocationSearch(search: string): void {
  Object.defineProperty(window, "location", {
    value: { ...originalLocation, search },
    writable: true,
    configurable: true,
  });
}

function restoreLocation(): void {
  Object.defineProperty(window, "location", {
    value: originalLocation,
    writable: true,
    configurable: true,
  });
}

/** Helper: assert a query result is non-null and return it narrowed. Keeps
 * the test bodies readable without spraying `!` (Biome forbids non-null
 * assertions) or chaining `?.` everywhere downstream. */
function expectQuery<E extends Element>(root: ParentNode, selector: string): E {
  const el = root.querySelector<E>(selector);
  if (el === null) {
    throw new Error(`Expected query "${selector}" to match an element`);
  }
  return el;
}

describe("DevControls", () => {
  beforeEach(() => {
    // Restore the store to its initial snapshot (including actions) so a
    // setter in one test can't leak forced state / motion into the next.
    useDevTweaksStore.setState(
      { ...initialStoreState, ...DEV_TWEAKS_DEFAULTS, forcedState: null },
      true,
    );
    window.localStorage.removeItem(DEV_TWEAKS_STORAGE_KEY);
  });

  afterEach(() => {
    restoreLocation();
    window.localStorage.removeItem(DEV_TWEAKS_STORAGE_KEY);
  });

  test("renders nothing when `?dev=1` is absent", () => {
    stubLocationSearch("?ui=new");
    const { container } = render(<DevControls />);
    expect(container.firstChild).toBeNull();
  });

  test("renders state pills + tweaks panel when `?dev=1` is present", () => {
    stubLocationSearch("?ui=new&dev=1");
    const { container } = render(<DevControls />);

    // Six pills, one per state.
    const pills = container.querySelectorAll(".state-pills .pill");
    expect(pills).toHaveLength(6);
    const labels = Array.from(pills).map((p) => p.getAttribute("data-state"));
    expect(labels).toEqual(["idle", "listen", "think", "speak", "alert", "error"]);

    // Tweaks panel hosts the sliders + selects + toggle.
    expect(container.querySelector(".twk-panel")).not.toBeNull();
    expect(container.querySelector('input[type="range"][aria-label="Motion"]')).not.toBeNull();
    expect(container.querySelector('input[type="range"][aria-label="Glow"]')).not.toBeNull();
    expect(container.querySelector('select[aria-label="State"]')).not.toBeNull();
    expect(container.querySelector('select[aria-label="Variant"]')).not.toBeNull();
    expect(container.querySelector('select[aria-label="Mood"]')).not.toBeNull();
    expect(container.querySelector('select[aria-label="Theme"]')).not.toBeNull();
    expect(container.querySelector('button[aria-label="Auto-cycle"]')).not.toBeNull();
  });

  test("clicking the `think` pill forces the sphere state to `think`", () => {
    stubLocationSearch("?dev=1");
    const { container } = render(<DevControls />);
    const thinkPill = expectQuery<HTMLButtonElement>(container, '.pill[data-state="think"]');
    fireEvent.click(thinkPill);
    expect(useDevTweaksStore.getState().forcedState).toBe("think");
    // The `.on` class flips on the active pill so the CSS accent kicks in.
    expect(thinkPill.classList.contains("on")).toBe(true);
  });

  test("sliding `motion` to 0.3 updates the store", () => {
    stubLocationSearch("?dev=1");
    const { container } = render(<DevControls />);
    const slider = expectQuery<HTMLInputElement>(
      container,
      'input[type="range"][aria-label="Motion"]',
    );
    fireEvent.change(slider, { target: { value: "0.3" } });
    expect(useDevTweaksStore.getState().motion).toBeCloseTo(0.3, 5);
  });

  test("persists motion to localStorage and re-hydrates on a fresh module load", async () => {
    stubLocationSearch("?dev=1");
    // First mount: change motion → store writes the snapshot.
    useDevTweaksStore.getState().setMotion(0.3);
    const raw = window.localStorage.getItem(DEV_TWEAKS_STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw ?? "{}") as { motion?: number };
    expect(parsed.motion).toBeCloseTo(0.3, 5);

    // Reset modules and re-import the store: hydration should pull motion=0.3
    // back out of localStorage, mirroring a reload of the app.
    vi.resetModules();
    const fresh = await import("../../state/devTweaksStore");
    expect(fresh.useDevTweaksStore.getState().motion).toBeCloseTo(0.3, 5);
    // forcedState is intentionally NOT persisted — a fresh hydration always
    // starts with the production derivation back in charge.
    expect(fresh.useDevTweaksStore.getState().forcedState).toBeNull();
  });

  test("pressing `3` forces the state to `think` (keyboard mapping)", () => {
    stubLocationSearch("?dev=1");
    render(<DevControls />);
    fireEvent.keyDown(window, { key: "3" });
    expect(useDevTweaksStore.getState().forcedState).toBe("think");
  });

  test("each key 1-6 maps to the matching state (idle..error)", () => {
    stubLocationSearch("?dev=1");
    render(<DevControls />);
    const pairs: Array<[string, string]> = [
      ["1", "idle"],
      ["2", "listen"],
      ["3", "think"],
      ["4", "speak"],
      ["5", "alert"],
      ["6", "error"],
    ];
    for (const [key, expected] of pairs) {
      fireEvent.keyDown(window, { key });
      expect(useDevTweaksStore.getState().forcedState).toBe(expected);
    }
  });

  test("pressing `3` while an INPUT is focused does NOT force state", () => {
    stubLocationSearch("?dev=1");
    render(<DevControls />);

    // Mount a real input, focus it, then dispatch the keydown on the input.
    // The handler's tagName guard should bail before calling `setForcedState`.
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    fireEvent.keyDown(input, { key: "3" });
    expect(useDevTweaksStore.getState().forcedState).toBeNull();

    document.body.removeChild(input);
  });

  test("pressing `3` while a TEXTAREA is focused does NOT force state", () => {
    stubLocationSearch("?dev=1");
    render(<DevControls />);

    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    ta.focus();

    fireEvent.keyDown(ta, { key: "3" });
    expect(useDevTweaksStore.getState().forcedState).toBeNull();

    document.body.removeChild(ta);
  });

  test("non-state keys (7, a, q) are ignored", () => {
    stubLocationSearch("?dev=1");
    render(<DevControls />);
    fireEvent.keyDown(window, { key: "7" });
    fireEvent.keyDown(window, { key: "a" });
    fireEvent.keyDown(window, { key: "q" });
    expect(useDevTweaksStore.getState().forcedState).toBeNull();
  });

  test("selecting a different variant updates the store", () => {
    stubLocationSearch("?dev=1");
    const { container } = render(<DevControls />);
    const select = expectQuery<HTMLSelectElement>(container, 'select[aria-label="Variant"]');
    fireEvent.change(select, { target: { value: "3" } });
    expect(useDevTweaksStore.getState().variant).toBe(3);
  });

  test("switching mood persists and reflects in the store", () => {
    stubLocationSearch("?dev=1");
    const { container } = render(<DevControls />);
    const select = expectQuery<HTMLSelectElement>(container, 'select[aria-label="Mood"]');
    fireEvent.change(select, { target: { value: "normal" } });
    expect(useDevTweaksStore.getState().mood).toBe("normal");
    const raw = window.localStorage.getItem(DEV_TWEAKS_STORAGE_KEY);
    const parsed = JSON.parse(raw ?? "{}") as { mood?: string };
    expect(parsed.mood).toBe("normal");
  });

  test("toggling auto-cycle writes to the store + localStorage", () => {
    stubLocationSearch("?dev=1");
    const { container } = render(<DevControls />);
    const toggle = expectQuery<HTMLButtonElement>(container, 'button[aria-label="Auto-cycle"]');
    expect(toggle.getAttribute("data-on")).toBe("0");
    fireEvent.click(toggle);
    expect(useDevTweaksStore.getState().autoCycle).toBe(true);
    const raw = window.localStorage.getItem(DEV_TWEAKS_STORAGE_KEY);
    const parsed = JSON.parse(raw ?? "{}") as { autoCycle?: boolean };
    expect(parsed.autoCycle).toBe(true);
    // Cleanup: turn off so the interval doesn't outlive the test (the
    // unmount via the test runner cleans it up anyway, but be explicit).
    fireEvent.click(toggle);
    expect(useDevTweaksStore.getState().autoCycle).toBe(false);
  });
});
