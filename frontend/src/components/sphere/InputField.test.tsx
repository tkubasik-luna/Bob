import { fireEvent, render, screen } from "@testing-library/react";
import { type Mock, beforeEach, describe, expect, test, vi } from "vitest";
import type { ClientMessage } from "../../types/ws";

// Voice mode mock — lets us flip the toggle per test so we can assert the
// `voice` flag piggybacks on the WS frame. Default OFF here so the existing
// assertions don't have to opt out of voice; the real hook defaults ON but
// tests are self-contained and explicit about which branch they exercise.
const voiceEnabledRef = vi.hoisted(() => ({ value: false }));
vi.mock("../../hooks/useVoiceMode", () => ({
  useVoiceMode: () => ({
    voiceEnabled: voiceEnabledRef.value,
    toggle: () => {
      voiceEnabledRef.value = !voiceEnabledRef.value;
    },
  }),
}));

import { useChatStore } from "../../store/chatStore";
import { InputField } from "./InputField";
import { SphereWsContext } from "./sphereWsContext";

const initialState = useChatStore.getState();

const sendSpy: Mock<(msg: ClientMessage) => void> = vi.fn();

function renderInputField() {
  return render(
    <SphereWsContext.Provider value={sendSpy}>
      <InputField />
    </SphereWsContext.Provider>,
  );
}

function lastSentMessage(): ClientMessage | undefined {
  const calls = sendSpy.mock.calls;
  return calls.length > 0 ? (calls.at(-1)?.[0] as ClientMessage) : undefined;
}

describe("InputField", () => {
  beforeEach(() => {
    useChatStore.setState(initialState, true);
    // The component now reads `connectionStatus` directly from the store
    // (since the local `useWebSocket` hook is gone — the connection lives at
    // the top of the `?ui=new` tree). Seed it to "open" so submits go through.
    useChatStore.setState({ connectionStatus: "open" });
    sendSpy.mockReset();
    voiceEnabledRef.value = false;
  });

  test("typing + Enter sends the message and clears the textarea", () => {
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");

    fireEvent.change(textarea, { target: { value: "salut Bob" } });
    expect(textarea.value).toBe("salut Bob");

    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(lastSentMessage()).toEqual({ type: "user_msg", content: "salut Bob" });
    expect(textarea.value).toBe("");
  });

  test("Enter appends the user message to the store (same path as ChatView)", () => {
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    const stored = useChatStore.getState().messages;
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ role: "user", content: "ping" });
  });

  test("Shift+Enter does NOT submit and leaves the textarea in the user's hands", () => {
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");

    fireEvent.change(textarea, { target: { value: "line 1" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    // Submit must not fire — the browser's default newline insertion is
    // delegated to the textarea (jsdom doesn't actually mutate the value
    // here, but the contract is "we don't preventDefault and we don't
    // submit"). Assert both halves.
    expect(sendSpy).not.toHaveBeenCalled();
    expect(textarea.value).toBe("line 1");
  });

  test("Enter with empty value is a no-op", () => {
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(sendSpy).not.toHaveBeenCalled();
    expect(useChatStore.getState().messages).toHaveLength(0);
  });

  test("Enter with whitespace-only value is a no-op", () => {
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");
    fireEvent.change(textarea, { target: { value: "   \n  " } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(sendSpy).not.toHaveBeenCalled();
    expect(useChatStore.getState().messages).toHaveLength(0);
  });

  test("voice flag piggybacks on the WS frame when voice mode is enabled", () => {
    voiceEnabledRef.value = true;
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");
    fireEvent.change(textarea, { target: { value: "speak this" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(lastSentMessage()).toEqual({ type: "user_msg", content: "speak this", voice: true });
  });

  test("trims surrounding whitespace before dispatch", () => {
    renderInputField();
    const textarea = screen.getByPlaceholderText<HTMLTextAreaElement>("Tapez pour parler à Bob");
    fireEvent.change(textarea, { target: { value: "   bonjour   " } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(lastSentMessage()).toEqual({ type: "user_msg", content: "bonjour" });
  });
});
