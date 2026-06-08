import "@testing-library/jest-dom/vitest";

// Vitest + jsdom 29 ships without a real `localStorage` implementation. Install
// a minimal in-memory polyfill for every test (idempotent — skips when a real
// one exists). Components that persist UI state (SetupScreen's setup_complete
// flag, devTweaksStore) rely on it; without this they throw at runtime in tests.
// Note: a module that reads localStorage at IMPORT time (devTweaksStore) still
// needs its own `vi.hoisted` polyfill, since setupFiles run after that file's
// imports are evaluated — this shared one covers the common runtime-access case.
(() => {
  if (typeof window === "undefined") return;
  // biome-ignore lint/suspicious/noExplicitAny: feature-detect without dragging Storage into scope
  const w = window as any;
  if (typeof w.localStorage !== "undefined" && w.localStorage !== null) return;
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    value: {
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
    },
    writable: true,
    configurable: true,
  });
})();
