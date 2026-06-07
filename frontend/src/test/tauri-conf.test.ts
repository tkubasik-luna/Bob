import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

/**
 * Tauri configuration regression tests (#0036).
 *
 * The `?ui=new` Sphere window runs borderless (`decorations: false`) so the
 * cinematic look isn't broken by OS chrome — the user drags it via the
 * 28px CSS drag region rendered by `SphereUI`. The legacy `?ui=legacy`
 * (`ChatView`) window was decommissioned once the Piste 3D HUD shipped, so
 * the config must no longer declare it. These tests read the actual
 * `tauri.conf.json` so the Tauri runtime config stays in sync with the
 * frontend assumptions.
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

describe("Info.plist (AEC spike — issue 0097, mic entitlement)", () => {
  // Criterion 1 of PRD 0016 Annexe I needs NSMicrophoneUsageDescription in the
  // bundled Info.plist, or macOS terminates the app on first getUserMedia audio
  // access. Tauri v2 auto-merges src-tauri/Info.plist into the bundle. This
  // regression test keeps the key present (a plain-text scan — no plist parser
  // dependency, the file is a tiny known-shape XML).
  function loadInfoPlist(): string {
    const path = resolve(__dirname, "../../src-tauri/Info.plist");
    return readFileSync(path, "utf-8");
  }

  test("declares NSMicrophoneUsageDescription with a non-empty purpose string", () => {
    const plist = loadInfoPlist();
    expect(plist).toContain("<key>NSMicrophoneUsageDescription</key>");
    // The key must be followed by a non-empty <string> purpose (Apple rejects
    // an empty usage description at submission and the prompt looks broken).
    const match = plist.match(
      /<key>NSMicrophoneUsageDescription<\/key>\s*<string>([^<]+)<\/string>/,
    );
    expect(match).not.toBeNull();
    expect((match?.[1] ?? "").trim().length).toBeGreaterThan(0);
  });
});

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

  test("the legacy window is gone; `new` + `debug` remain", () => {
    const config = loadTauriConfig();
    const labels = config.app.windows.map((w) => w.label);
    expect(labels).not.toContain("legacy");
    expect(labels).toContain("new");
    expect(labels).toContain("debug");
  });
});
