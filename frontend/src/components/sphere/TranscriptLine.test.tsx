import { render } from "@testing-library/react";
import { beforeEach, describe, expect, test } from "vitest";
import { useChatStore } from "../../store/chatStore";
import type { ChatMessage } from "../../types/ws";
import { TranscriptLine } from "./TranscriptLine";

// Snapshot the pristine store so each test starts from a clean slate — same
// pattern as `HudTasks.test.tsx` / `useSphereState.test.ts`.
const initialState = useChatStore.getState();

function setMessages(messages: ChatMessage[]): void {
  useChatStore.setState({ messages });
}

describe("TranscriptLine", () => {
  beforeEach(() => {
    useChatStore.setState(initialState, true);
  });

  test("idle + no messages → renders the French hint", () => {
    const { container } = render(<TranscriptLine state="idle" />);
    const hint = container.querySelector(".hud-transcript-hint");
    expect(hint).not.toBeNull();
    expect(hint?.textContent).toBe("Tapez pour parler à Bob");
    // Sanity: thinking dots are NOT mounted in this slot.
    expect(container.querySelector(".hud-transcript-thinking")).toBeNull();
  });

  test("think → renders the animated `thinking · · ·` block (not raw user text)", () => {
    setMessages([{ id: "u1", role: "user", content: "Que fait Bob ?" }]);
    const { container } = render(<TranscriptLine state="think" />);
    const thinking = container.querySelector(".hud-transcript-thinking");
    expect(thinking).not.toBeNull();
    // The mockup's three-dot rhythm uses `.dot.d1 / d2 / d3`.
    expect(container.querySelectorAll(".hud-transcript-thinking .dot")).toHaveLength(3);
    // The hint must not coexist with the thinking block.
    expect(container.querySelector(".hud-transcript-hint")).toBeNull();
  });

  test("speak + assistant ≤ 80 chars → renders the message verbatim", () => {
    const short = "Il est 14:32.";
    setMessages([
      { id: "u1", role: "user", content: "Quelle heure est-il ?" },
      { id: "a1", role: "assistant", content: short },
    ]);
    const { container } = render(<TranscriptLine state="speak" />);
    const text = container.querySelector(".hud-transcript-text");
    expect(text).not.toBeNull();
    expect(text?.textContent).toBe(short);
  });

  test("speak + assistant > 80 chars → truncates to 80 + ellipsis", () => {
    // Build a deterministic 120-char payload so we can assert the slice.
    const long = "a".repeat(120);
    setMessages([{ id: "a1", role: "assistant", content: long }]);
    const { container } = render(<TranscriptLine state="speak" />);
    const text = container.querySelector(".hud-transcript-text");
    expect(text).not.toBeNull();
    expect(text?.textContent).toBe(`${"a".repeat(80)}…`);
    expect(text?.textContent?.length).toBe(81); // 80 chars + 1 ellipsis glyph
  });

  test("speak with no assistant message yet → falls back to hint", () => {
    setMessages([{ id: "u1", role: "user", content: "Hello" }]);
    const { container } = render(<TranscriptLine state="speak" />);
    expect(container.querySelector(".hud-transcript-hint")).not.toBeNull();
    expect(container.querySelector(".hud-transcript-text")).toBeNull();
  });

  test("idle after a turn → shows the assistant snippet, not the hint", () => {
    setMessages([
      { id: "u1", role: "user", content: "Salut" },
      { id: "a1", role: "assistant", content: "Bonjour !" },
    ]);
    const { container } = render(<TranscriptLine state="idle" />);
    const text = container.querySelector(".hud-transcript-text");
    expect(text?.textContent).toBe("Bonjour !");
    expect(container.querySelector(".hud-transcript-hint")).toBeNull();
  });

  test("hidden=true → renders nothing (overlay path)", () => {
    setMessages([{ id: "a1", role: "assistant", content: "Hello" }]);
    const { container } = render(<TranscriptLine state="speak" hidden />);
    // Component returns null, so the wrapper has no children at all.
    expect(container.firstChild).toBeNull();
  });

  test("multi-turn: surfaces the most recent assistant message, not the first", () => {
    setMessages([
      { id: "a1", role: "assistant", content: "First reply" },
      { id: "u1", role: "user", content: "follow-up" },
      { id: "a2", role: "assistant", content: "Second reply" },
    ]);
    const { container } = render(<TranscriptLine state="speak" />);
    expect(container.querySelector(".hud-transcript-text")?.textContent).toBe("Second reply");
  });

  test("error state with no assistant → hint fallback (transcript stays quiet)", () => {
    const { container } = render(<TranscriptLine state="error" />);
    expect(container.querySelector(".hud-transcript-hint")).not.toBeNull();
    expect(container.querySelector(".hud-transcript-thinking")).toBeNull();
  });
});
