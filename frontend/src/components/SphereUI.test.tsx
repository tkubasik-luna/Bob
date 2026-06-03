import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Vitest 4 + jsdom 29 ships without `localStorage`; the dev tweaks store
// reads from it on init, so install a minimal in-memory polyfill BEFORE any
// import resolves. Same pattern as `DevControls.test.tsx`.
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

// Capture every constructed mock WebSocket so the SphereUI mount path
// doesn't open a real one. Same shape as `useChatWsBridge.test.ts`.
const sockets = vi.hoisted(() => ({ list: [] as MockSocket[] }));

class MockSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  url: string;
  readyState: number = MockSocket.CONNECTING;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    sockets.list.push(this);
  }
}

import { useChatStore } from "../store/chatStore";
import { SphereUI } from "./SphereUI";

const initialState = useChatStore.getState();

let originalWebSocket: typeof WebSocket;
const originalGetContext = HTMLCanvasElement.prototype.getContext;

// Minimal WebGL2 stub for the placeholder `SphereCanvas` — it only checks a
// handful of methods exist on the gl context before the rAF loop runs. We don't
// care about render output; only that the shell mounts without throwing.
function installGlStub(): void {
  const noop = (): void => undefined;
  // biome-ignore lint/suspicious/noExplicitAny: jsdom getContext lacks WebGL types
  (HTMLCanvasElement.prototype as any).getContext = function (
    this: HTMLCanvasElement,
    type: string,
  ): unknown {
    if (type === "webgl2") {
      return {
        VERTEX_SHADER: 1,
        FRAGMENT_SHADER: 2,
        ARRAY_BUFFER: 3,
        STATIC_DRAW: 4,
        TRIANGLES: 5,
        FLOAT: 6,
        COMPILE_STATUS: 7,
        LINK_STATUS: 8,
        createShader: () => ({}),
        shaderSource: noop,
        compileShader: noop,
        getShaderParameter: () => true,
        getShaderInfoLog: () => "",
        createProgram: () => ({}),
        attachShader: noop,
        linkProgram: noop,
        getProgramParameter: () => true,
        getProgramInfoLog: () => "",
        useProgram: noop,
        createBuffer: () => ({}),
        bindBuffer: noop,
        bufferData: noop,
        getAttribLocation: () => 0,
        vertexAttribPointer: noop,
        enableVertexAttribArray: noop,
        getUniformLocation: () => ({}),
        uniform1f: noop,
        uniform1i: noop,
        uniform2f: noop,
        uniform3f: noop,
        viewport: noop,
        clearColor: noop,
        clear: noop,
        drawArrays: noop,
      };
    }
    // 2D path for the glyph overlay — same noop shape as the SphereCanvas test.
    return {
      clearRect: noop,
      save: noop,
      restore: noop,
      translate: noop,
      fillText: noop,
      set font(_v: string) {},
      set textAlign(_v: string) {},
      set textBaseline(_v: string) {},
      set fillStyle(_v: string) {},
      set shadowColor(_v: string) {},
      set shadowBlur(_v: number) {},
      // biome-ignore lint/suspicious/noExplicitAny: minimal 2D ctx stub
    } as any;
  };
}

// PRD 0014 / issue 0083 — the `?ui=new` window now renders the « Piste 3D ·
// Nacre » shell. This is a light jsdom render test: it asserts the shell
// STRUCTURE (root, slots, identity, bottom input) is wired correctly so the
// downstream slot issues (0084–0091) have a stable scaffold to build on. It
// does NOT exercise WebGL or CSS. The auto-open overlay behaviour the previous
// suite covered is gone (the PRD moved to click-only overlay, issue 0088), so
// those assertions are replaced by shell-structure assertions here.
describe("SphereUI — piste shell structure", () => {
  beforeEach(() => {
    originalWebSocket = globalThis.WebSocket;
    sockets.list.length = 0;
    // biome-ignore lint/suspicious/noExplicitAny: minimal WS stub for the test
    globalThis.WebSocket = MockSocket as any;
    useChatStore.setState(initialState, true);
    installGlStub();
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
    HTMLCanvasElement.prototype.getContext = originalGetContext;
    vi.restoreAllMocks();
  });

  test("renders the .piste root carrying the foundation layout modifiers", () => {
    const { container } = render(<SphereUI />);
    const root = container.querySelector(".piste");
    expect(root).not.toBeNull();
    // Foundation modifier classes the scoped CSS keys off of.
    expect(root?.classList.contains("layout-depth")).toBe(true);
    expect(root?.classList.contains("cam-deep")).toBe(true);
    expect(root?.classList.contains("panel-frost")).toBe(true);
  });

  test("renders the Tauri drag region as the first child of the .piste root (#0036)", () => {
    // The borderless `?ui=new` Tauri window has `decorations: false`, so the
    // user needs a 28px transparent strip up top to move it. The styling
    // (`-webkit-app-region: drag`) lives in p3d.css; jsdom doesn't surface
    // webkit-only CSS via getComputedStyle, so we assert the structural
    // contract (exists, right class, FIRST child so it sits above the canvas).
    const { container } = render(<SphereUI />);
    const root = container.querySelector(".piste");
    expect(root).not.toBeNull();
    const dragRegion = root?.querySelector(".drag-region");
    expect(dragRegion).toBeInstanceOf(HTMLDivElement);
    expect(root?.firstElementChild).toBe(dragRegion);
  });

  test("renders the background + grain layers", () => {
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".piste-bg")).not.toBeNull();
    expect(container.querySelector(".piste-grain")).not.toBeNull();
  });

  test("renders the top-left identity mark + tagline", () => {
    const { container } = render(<SphereUI />);
    const id = container.querySelector(".piste-id");
    expect(id).not.toBeNull();
    expect(container.querySelector(".id-name")?.textContent).toBe("BOB");
    // Default (idle, no messages, socket not yet open → "erreur") — assert the
    // mark renders SOME French état word rather than the exact one, so the test
    // doesn't couple to the connection-state derivation.
    expect(container.querySelector(".id-state")?.textContent).toMatch(/^· \S+/);
    expect(container.querySelector(".id-tagline")?.textContent).toBe(
      "nacre — sphère liquide · sanctuaire en profondeur",
    );
  });

  test("renders the 3D stage with the three depth slots (core has the orb)", () => {
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".stage-3d .stage-cam")).not.toBeNull();
    expect(container.querySelector(".slot-task")).not.toBeNull();
    expect(container.querySelector(".slot-core")).not.toBeNull();
    expect(container.querySelector(".slot-data")).not.toBeNull();
    // The core slot holds the conscience NEBULA orb (issue 0084 swapped the
    // foundation's SphereCanvas placeholder for `.core-nebula` → `.cv-canvas`),
    // plus the CORE · conscience label. WebGL2 is unavailable under jsdom, so
    // the orb falls back to the `.hud-error` banner — either the canvas or the
    // banner is acceptable proof the orb mounted in the slot.
    const orbMounted =
      container.querySelector(".slot-core .core-nebula .cv-canvas") !== null ||
      container.querySelector(".slot-core .core-nebula .hud-error") !== null;
    expect(orbMounted).toBe(true);
    expect(container.querySelector(".slot-core .core-label")?.textContent).toBe(
      "CORE · conscience",
    );
  });

  test("renders the provisional bottom input (transcript + field)", () => {
    const { container } = render(<SphereUI />);
    // Transcript line + input field live in the bottom zone. The mute toggle
    // renders as a sibling (self-positioned fixed element).
    expect(container.querySelector(".hud-zone.b .hud-transcript")).not.toBeNull();
    expect(container.querySelector(".hud-zone.b .hud-input")).not.toBeNull();
    expect(container.querySelector(".hud-mute")).not.toBeNull();
  });

  test("does NOT auto-open the overlay (click-only per PRD 0014)", () => {
    // The shell mounts with no overlay, and an overlay-worthy assistant message
    // must NOT auto-open it anymore — the overlay opens only via a slot's
    // `openOverlay` callback (issue 0088). Regression guard against the removed
    // auto-open effects.
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".overlay-card")).toBeNull();
  });
});
