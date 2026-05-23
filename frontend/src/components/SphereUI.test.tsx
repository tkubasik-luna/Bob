import { act, render } from "@testing-library/react";
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
import type { ChatMessage } from "../types/ws";
import { SphereUI } from "./SphereUI";

const initialState = useChatStore.getState();

let originalWebSocket: typeof WebSocket;
const originalGetContext = HTMLCanvasElement.prototype.getContext;

// Minimal WebGL2 stub for `SphereCanvas` — it only checks a handful of
// methods exist on the gl context before the rAF loop runs. We don't care
// about render output for the integration test, only that the component
// mounts without throwing.
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

function makeAssistantMessage(id: string, content: string): ChatMessage {
  return { id, role: "assistant", content };
}

describe("SphereUI — overlay auto-trigger integration", () => {
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

  test("an overlay-worthy assistant message opens the MarkdownOverlay", () => {
    const { container } = render(<SphereUI />);
    // No overlay until an assistant message appears.
    expect(container.querySelector(".overlay-card")).toBeNull();

    act(() => {
      useChatStore.setState({
        messages: [
          makeAssistantMessage(
            "a1",
            "# Heading\n\nlong content with **structure** and lists:\n\n- one\n- two\n- three",
          ),
        ],
      });
    });

    expect(container.querySelector(".overlay-card")).not.toBeNull();
    // The transcript line is hidden while the overlay carries the context.
    expect(container.querySelector(".hud-transcript")).toBeNull();
  });

  test("a short plain assistant message leaves the overlay closed", () => {
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        messages: [makeAssistantMessage("a1", "Il est 14:32.")],
      });
    });

    expect(container.querySelector(".overlay-card")).toBeNull();
    // The transcript line stays mounted in this branch — short plain text
    // belongs there per the PRD.
    expect(container.querySelector(".hud-transcript")).not.toBeNull();
  });

  test("renders the Tauri drag region as the first child of the .app wrapper (#0036)", () => {
    // The borderless `?ui=new` Tauri window has `decorations: false`, so the
    // user needs a 28px transparent strip up top to move it. The styling
    // (`-webkit-app-region: drag`) lives in `hud.css`; jsdom doesn't surface
    // webkit-only CSS properties via `getComputedStyle`, so we assert the
    // structural contract (the element exists, has the expected class, and
    // is the FIRST child of `.app` so it sits in the right stacking order).
    const { container } = render(<SphereUI />);
    const appRoot = container.querySelector(".app");
    expect(appRoot).not.toBeNull();
    const dragRegion = appRoot?.querySelector(".drag-region");
    expect(dragRegion).not.toBeNull();
    expect(dragRegion).toBeInstanceOf(HTMLDivElement);
    // FIRST-child contract: must come before SphereCanvas so the drag layer
    // sits above the canvas in the stacking order (z-index: 100 in hud.css).
    expect(appRoot?.firstElementChild).toBe(dragRegion);
  });
});
