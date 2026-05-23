import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

/**
 * Tauri configuration regression tests (#0036).
 *
 * The `?ui=new` Sphere window runs borderless (`decorations: false`) so the
 * cinematic look isn't broken by OS chrome — the user drags it via the
 * 28px CSS drag region rendered by `SphereUI`. The `?ui=legacy` window must
 * keep its native chrome so the legacy `ChatView` remains usable during the
 * dev transition. These tests read the actual `tauri.conf.json` so the
 * Tauri runtime config stays in sync with the frontend assumptions.
 *
 * NOTE: vitest's `include` glob is `src/**` so this file lives under
 * `frontend/src/test/`, not next to `tauri.conf.json` itself. The path
 * resolves relative to `frontend/` (vitest cwd).
 */

interface TauriWindow {
  label: string;
  decorations?: boolean;
  transparent?: boolean;
}

interface TauriConfig {
  app: {
    windows: TauriWindow[];
  };
}

function loadTauriConfig(): TauriConfig {
  const path = resolve(__dirname, "../../src-tauri/tauri.conf.json");
  const raw = readFileSync(path, "utf-8");
  return JSON.parse(raw) as TauriConfig;
}

function findWindow(config: TauriConfig, label: string): TauriWindow {
  const win = config.app.windows.find((w) => w.label === label);
  if (!win) throw new Error(`window with label "${label}" not found`);
  return win;
}

describe("tauri.conf.json", () => {
  test("the `new` window is borderless (decorations: false)", () => {
    const config = loadTauriConfig();
    const win = findWindow(config, "new");
    expect(win.decorations).toBe(false);
  });

  test("the `new` window stays opaque (transparent: false or unset)", () => {
    const config = loadTauriConfig();
    const win = findWindow(config, "new");
    // The background app must remain opaque — the cinematic look comes from
    // the in-window CSS (`--bg`), not OS transparency. `transparent: false`
    // is explicit here but `undefined` (default) would also be acceptable.
    expect(win.transparent !== true).toBe(true);
  });

  test("the `legacy` window keeps its native OS chrome", () => {
    const config = loadTauriConfig();
    const win = findWindow(config, "legacy");
    // Either `decorations` is unset (Tauri default = true) or explicitly
    // `true`. The only forbidden value is `false`, which would strip the
    // legacy chrome we still rely on for the `ChatView` window.
    expect(win.decorations).not.toBe(false);
  });

  test("both `legacy` and `new` windows are declared", () => {
    const config = loadTauriConfig();
    const labels = config.app.windows.map((w) => w.label);
    expect(labels).toContain("legacy");
    expect(labels).toContain("new");
  });
});
