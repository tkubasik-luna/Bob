import { act, fireEvent, render } from "@testing-library/react";
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
import type { ChatMessage, Task } from "../types/ws";
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

function makeDoneTask(id: string, result: string, updatedAt = "2026-05-23T13:00:00Z"): Task {
  return {
    id,
    title: `Task ${id}`,
    goal: "test",
    state: "done",
    result,
    createdAt: "2026-05-23T12:58:00Z",
    updatedAt,
  };
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

  test("an overlay-worthy assistant message opens the SectionsOverlay", () => {
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

  test("assistant message with ui Markdown opens the overlay despite short speech", () => {
    // Regression: the streamed `ui_payload` frame is routed through the single
    // process-wide ws emitter (last-connected window wins), so the window that
    // asked the question can miss it. The closing `assistant_msg` still carries
    // the Markdown `ui`; the overlay must open from that even though the SPEECH
    // is a short intro `shouldOverlayResponse` rejects (see the test below).
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".overlay-card")).toBeNull();

    act(() => {
      useChatStore.setState({
        messages: [
          {
            id: "ui1",
            role: "assistant",
            content: "Voilà les grands moments :",
            ui: [
              {
                component: "Markdown",
                props: {
                  content: "## Bitcoin\n\n**2008** — whitepaper\n**2009** — genesis block",
                },
              },
            ],
          },
        ],
      });
    });

    expect(container.querySelector(".overlay-card")).not.toBeNull();
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

  test("a proactive (spoken-only) push never opens the overlay", () => {
    // Regression: a sub-task done synthesis arrives as a proactive
    // assistant_msg whose text is read aloud via TTS. Even when that text is
    // long/structured enough to trip `shouldOverlayResponse`, it must NOT
    // duplicate itself as a MarkdownOverlay card — the full result surfaces via
    // the task-result path instead.
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        messages: [
          {
            id: "p1",
            role: "assistant",
            content:
              "Voilà ce que j'ai trouvé à propos d'Ethereum…\n\nPlateforme de smart contracts.\nMigration vers le Proof of Stake.\nÉcosystème DeFi/NFT dominant.",
            proactive: true,
          },
        ],
      });
    });

    expect(container.querySelector(".overlay-card")).toBeNull();
  });

  test("dismissed overlay does not reopen on same assistant message", () => {
    // Regression: the open-trigger effect was keyed on `overlayContent` so
    // closing the overlay flipped it back to `null`, the effect re-fired
    // against the unchanged store, and the same message reopened the card —
    // an infinite reopen loop. Now we dedupe on the message id via a ref.
    const { container } = render(<SphereUI />);

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

    // User dismisses via global Esc. Overlay closes.
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();

    // Force a re-render against the SAME message (no store change). The
    // dedup ref must keep the overlay closed.
    act(() => {
      useChatStore.setState({
        messages: [...useChatStore.getState().messages],
      });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();
  });

  test("a NEW overlay-worthy message reopens the overlay after a prior dismiss", () => {
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        messages: [makeAssistantMessage("a1", "# First\n\n- one\n- two\n- three\n- four")],
      });
    });
    expect(container.querySelector(".overlay-card")).not.toBeNull();

    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();

    // A different assistant message lands. Dedup ref keys on id, so the new
    // id triggers re-evaluation and re-open.
    act(() => {
      useChatStore.setState({
        messages: [
          makeAssistantMessage("a1", "# First\n\n- one\n- two\n- three\n- four"),
          makeAssistantMessage("a2", "## Second\n\n| a | b |\n|---|---|\n| 1 | 2 |"),
        ],
      });
    });
    expect(container.querySelector(".overlay-card")).not.toBeNull();
  });

  test("subtask done with markdown result opens the overlay (#0004 follow-up)", () => {
    // The orchestrator stores long sub-task results on tasks[id].result, not on
    // the main `messages` stream. Without the task-side trigger, the overlay
    // never opens because the synth follow-up assistant message is short.
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        tasks: {
          t1: makeDoneTask(
            "t1",
            "# UK News\n\n- politics\n- economy\n- society\n\n| col | val |\n|---|---|\n| a | 1 |",
          ),
        },
      });
    });

    expect(container.querySelector(".overlay-card")).not.toBeNull();
  });

  test("subtask done with Mail resultPayload opens a Mail card in SectionsOverlay (issue 0067)", () => {
    // PRD 0010 / issue 0067 — a sub-agent that produced a STRUCTURED Mail
    // deliverable carries it on `tasks[id].resultPayload` (a LIST of section
    // descriptors). The task-result effect routes it through the unified
    // SectionsOverlay (`.surface-notes`); `Mail` is a `structured` section so
    // the overlay auto-opens unconditionally and renders the `MailCard` body.
    // The standalone MailOverlay is gone.
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".overlay-card")).toBeNull();

    act(() => {
      useChatStore.setState({
        tasks: {
          t1: {
            ...makeDoneTask("t1", "Mail de Marie, sujet 'Q3 forecast'"),
            resultPayload: [
              {
                component: "Mail",
                props: {
                  from: {
                    name: "Marie Lefèvre",
                    email: "marie.lefevre@lunabee.com",
                    role: "CFO · Lunabee",
                  },
                  receivedAt: "2026-05-28T14:22:00Z",
                  subject: "Q3 forecast — final review before Thursday",
                  bodyPreview: "Bob, can you have the deck ready by Thursday afternoon?",
                  flags: ["priority"],
                  attachments: [],
                  threadId: "thread-xyz-001",
                  messageId: "msg-xyz-001",
                  gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/thread-xyz-001",
                },
              },
            ],
          },
        },
      });
    });

    // The unified SectionsOverlay surfaces with the Mail card inside it.
    expect(container.querySelector(".overlay-card.surface-notes")).not.toBeNull();
    expect(container.querySelector(".ov-section .ov-email")).not.toBeNull();
    expect(container.querySelector(".ov-email-name")?.textContent).toBe("Marie Lefèvre");
  });

  test("'3 derniers mails' renders 3 stacked Mail cards in SectionsOverlay (issue 0067)", () => {
    // PRD 0010 / issue 0067 acceptance — the core bug fix: a multi-mail
    // resultPayload (one Mail section per message) renders as a vertical STACK
    // of MailCards inside the single SectionsOverlay, not just the first one.
    const mail = (subject: string, name: string, id: string) => ({
      component: "Mail" as const,
      props: {
        from: { name, email: `${name.toLowerCase()}@lunabee.com` },
        receivedAt: "2026-05-28T14:22:00Z",
        subject,
        bodyPreview: `Body of ${subject}`,
        flags: [],
        attachments: [],
        threadId: id,
        messageId: id,
        gmailWebUrl: `https://mail.google.com/mail/u/0/#inbox/${id}`,
      },
    });
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        tasks: {
          t1: {
            ...makeDoneTask("t1", "3 email(s) trouvé(s). Dernier : « mail A » de Alice."),
            resultPayload: [
              mail("mail A", "Alice", "m-a"),
              mail("mail B", "Bob", "m-b"),
              mail("mail C", "Carol", "m-c"),
            ],
          },
        },
      });
    });

    // One overlay shell, three Mail cards stacked inside it, in order.
    expect(container.querySelectorAll(".overlay-card").length).toBe(1);
    const cards = container.querySelectorAll(".ov-section .ov-email");
    expect(cards.length).toBe(3);
    const subjects = Array.from(container.querySelectorAll(".ov-section .ov-email-subject")).map(
      (el) => el.textContent,
    );
    expect(subjects).toEqual(["mail A", "mail B", "mail C"]);
  });

  test("a text-only Markdown section list applies the text heuristic — long content opens", () => {
    // PRD 0010 / issue 0066 — auto-open dispatch: a text-only list (Markdown
    // only) defers to `shouldOverlayResponse` on the section content. A long,
    // structured Markdown body passes the heuristic and opens the SectionsOverlay.
    const { container } = render(<SphereUI />);
    act(() => {
      useChatStore.setState({
        tasks: {
          t1: {
            ...makeDoneTask("t1", "short spoken summary"),
            resultPayload: [
              {
                component: "Markdown",
                props: { content: "# Rapport\n\n- a\n- b\n- c\n- d" },
              },
            ],
          },
        },
      });
    });
    expect(container.querySelector(".overlay-card.surface-notes")).not.toBeNull();
    expect(container.querySelector(".ov-section .md-h1")?.textContent).toBe("Rapport");
  });

  test("a text-only Markdown section list with trivial content does NOT open (heuristic)", () => {
    // The flip side: a short, unstructured Markdown body fails the heuristic so
    // the overlay stays closed (the spoken summary carries it instead).
    const { container } = render(<SphereUI />);
    act(() => {
      useChatStore.setState({
        tasks: {
          t1: {
            ...makeDoneTask("t1", "ok"),
            resultPayload: [{ component: "Markdown", props: { content: "ok" } }],
          },
        },
      });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();
  });

  test("dismissed task-result overlay does not reopen on same task", () => {
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        tasks: {
          t1: makeDoneTask("t1", "# Long\n\n- one\n- two\n- three\n- four"),
        },
      });
    });
    expect(container.querySelector(".overlay-card")).not.toBeNull();

    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();

    // Same task object update — must NOT reopen.
    act(() => {
      useChatStore.setState({
        tasks: { ...useChatStore.getState().tasks },
      });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();
  });

  test("clicking a done task in the HUD re-opens its result in the overlay", () => {
    // After auto-trigger + dismiss, the dedup ref blocks the auto path from
    // re-opening the same task. The HUD row's onClick bypasses dedup so the
    // user can re-visit any kept-in-FIFO task result.
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        tasks: {
          t1: makeDoneTask("t1", "# Kept\n\n- one\n- two\n- three\n- four"),
        },
      });
    });
    expect(container.querySelector(".overlay-card")).not.toBeNull();

    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(container.querySelector(".overlay-card")).toBeNull();

    // Task still rendered in HUD (no fade-out) and clickable.
    const row = container.querySelector('.hud-task[data-task-id="t1"]') as HTMLElement | null;
    expect(row).not.toBeNull();
    expect(row?.classList.contains("is-clickable")).toBe(true);

    act(() => {
      fireEvent.click(row as HTMLElement);
    });
    expect(container.querySelector(".overlay-card")).not.toBeNull();
  });

  test("dispatches a Mail assistant_msg.ui to a Mail card in SectionsOverlay (issue 0067)", () => {
    // A "Mail" descriptor in `assistant_msg.ui` renders as a `MailCard` inside
    // the unified SectionsOverlay (`.surface-notes`). `Mail` is `structured`, so
    // the overlay auto-opens regardless of the text heuristic. The standalone
    // MailOverlay (`.surface-email`) is gone.
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".overlay-card")).toBeNull();

    act(() => {
      useChatStore.setState({
        messages: [
          {
            id: "mail-1",
            role: "assistant",
            content: "Voilà l'email de Marie.",
            ui: [
              {
                component: "Mail",
                props: {
                  from: {
                    name: "Marie Lefèvre",
                    email: "marie.lefevre@lunabee.com",
                    role: "CFO · Lunabee",
                  },
                  receivedAt: "2026-05-28T14:22:00Z",
                  subject: "Q3 forecast — final review before Thursday",
                  bodyPreview: "Bob, can you have the deck ready by Thursday afternoon?",
                  flags: ["priority"],
                  attachments: [
                    {
                      name: "Q3-forecast-v4.pdf",
                      sizeBytes: 2_400_000,
                      mime: "application/pdf",
                    },
                  ],
                  threadId: "thread-xyz-001",
                  messageId: "msg-xyz-001",
                  gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/thread-xyz-001",
                },
              },
            ],
          },
        ],
      });
    });

    // The unified SectionsOverlay (`.surface-notes`) mounts with the Mail card.
    expect(container.querySelector(".overlay-card.surface-notes")).not.toBeNull();
    expect(container.querySelector(".ov-section .ov-email")).not.toBeNull();
    // Body content matches the fixture.
    expect(container.querySelector(".ov-email-name")?.textContent).toBe("Marie Lefèvre");
  });

  test("dispatches a Markdown assistant_msg.ui to SectionsOverlay (issue 0053 regression)", () => {
    // Companion to the Mail dispatch test: a "Markdown" descriptor must
    // still route to the sections surface after the dispatcher refactor.
    const { container } = render(<SphereUI />);

    act(() => {
      useChatStore.setState({
        messages: [
          {
            id: "md-1",
            role: "assistant",
            content: "Voilà.",
            ui: [
              {
                component: "Markdown",
                props: {
                  content: "## Bitcoin\n\n**2008** — whitepaper",
                },
              },
            ],
          },
        ],
      });
    });

    expect(container.querySelector(".overlay-card.surface-notes")).not.toBeNull();
    // A text-only Markdown list carries no Mail card.
    expect(container.querySelector(".ov-section .ov-email")).toBeNull();
  });

  test("dispatches a streaming Mail ui_payload to a Mail card in SectionsOverlay (issue 0067)", () => {
    // Streaming `ui_payload` path: the `streamingAssistant.ui` field on the
    // chat store carries the descriptor mid-turn (before the closing
    // `assistant_msg`). It must surface as a MailCard in SectionsOverlay too.
    const { container } = render(<SphereUI />);
    expect(container.querySelector(".overlay-card")).toBeNull();

    act(() => {
      useChatStore.setState({
        streamingAssistant: {
          msgId: "stream-mail-1",
          speech: "Voilà l'email de Marie.",
          ui: {
            component: "Mail",
            props: {
              from: { name: "Marie Lefèvre", email: "marie.lefevre@lunabee.com" },
              receivedAt: "2026-05-28T14:22:00Z",
              subject: "Q3 forecast",
              bodyPreview: "Hi Bob",
              flags: [],
              attachments: [],
              threadId: "thread-stream-1",
              messageId: "msg-stream-1",
              gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/thread-stream-1",
            },
          },
        },
      });
    });

    expect(container.querySelector(".overlay-card.surface-notes")).not.toBeNull();
    expect(container.querySelector(".ov-section .ov-email")).not.toBeNull();
    expect(container.querySelector(".ov-email-name")?.textContent).toBe("Marie Lefèvre");
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
