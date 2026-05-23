import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

// `useVoiceMode` keeps its `voiceEnabled` state inside the hook closure (one
// boolean per `useState` per mount). To assert what the component does with
// the state without coupling the test to React internals, we mock the hook
// and back it with a hoisted ref so each test can flip `voiceEnabled` ahead
// of `render(...)` AND spy on `toggle`. The default values match the real
// hook's initial state (`voiceEnabled = false`).
const voiceModeMock = vi.hoisted(() => ({
  voiceEnabled: false,
  toggle: vi.fn(),
}));
vi.mock("../../hooks/useVoiceMode", () => ({
  useVoiceMode: () => ({
    voiceEnabled: voiceModeMock.voiceEnabled,
    toggle: voiceModeMock.toggle,
  }),
}));

import { MuteToggle } from "./MuteToggle";

describe("MuteToggle", () => {
  beforeEach(() => {
    voiceModeMock.voiceEnabled = false;
    voiceModeMock.toggle.mockReset();
  });

  test("renders the speaker-on icon when voiceEnabled=true", () => {
    voiceModeMock.voiceEnabled = true;
    render(<MuteToggle />);
    expect(screen.getByTestId("speaker-on-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("speaker-off-icon")).not.toBeInTheDocument();
  });

  test("renders the speaker-off icon when voiceEnabled=false", () => {
    voiceModeMock.voiceEnabled = false;
    render(<MuteToggle />);
    expect(screen.getByTestId("speaker-off-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("speaker-on-icon")).not.toBeInTheDocument();
  });

  test("clicking the button calls toggle()", () => {
    render(<MuteToggle />);
    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    expect(voiceModeMock.toggle).toHaveBeenCalledTimes(1);
  });

  test("aria-pressed reflects voiceEnabled (true)", () => {
    voiceModeMock.voiceEnabled = true;
    render(<MuteToggle />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
  });

  test("aria-pressed reflects voiceEnabled (false)", () => {
    voiceModeMock.voiceEnabled = false;
    render(<MuteToggle />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "false");
  });

  test("pressing `m` on window calls toggle()", () => {
    render(<MuteToggle />);
    fireEvent.keyDown(window, { key: "m" });
    expect(voiceModeMock.toggle).toHaveBeenCalledTimes(1);
  });

  test("pressing `M` (uppercase) on window also calls toggle()", () => {
    render(<MuteToggle />);
    fireEvent.keyDown(window, { key: "M" });
    expect(voiceModeMock.toggle).toHaveBeenCalledTimes(1);
  });

  test("pressing `m` while an INPUT is focused does NOT call toggle()", () => {
    render(<MuteToggle />);

    // Mount a real input, focus it, then dispatch the keydown. The
    // listener attached on `window` still receives the bubbled event, but
    // the `document.activeElement` guard should short-circuit before the
    // toggle fires.
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    fireEvent.keyDown(input, { key: "m" });
    expect(voiceModeMock.toggle).not.toHaveBeenCalled();

    document.body.removeChild(input);
  });

  test("pressing `m` while a TEXTAREA is focused does NOT call toggle()", () => {
    render(<MuteToggle />);

    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    ta.focus();

    fireEvent.keyDown(ta, { key: "m" });
    expect(voiceModeMock.toggle).not.toHaveBeenCalled();

    document.body.removeChild(ta);
  });

  test("non-`m` keys are ignored", () => {
    render(<MuteToggle />);
    fireEvent.keyDown(window, { key: "a" });
    fireEvent.keyDown(window, { key: "n" });
    fireEvent.keyDown(window, { key: "1" });
    expect(voiceModeMock.toggle).not.toHaveBeenCalled();
  });

  test("unmount detaches the window keydown listener", () => {
    const { unmount } = render(<MuteToggle />);
    unmount();
    fireEvent.keyDown(window, { key: "m" });
    expect(voiceModeMock.toggle).not.toHaveBeenCalled();
  });
});
