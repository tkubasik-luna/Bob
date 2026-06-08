import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The SetupScreen talks to the backend through `lib/llmApi`; mock it so the
// test is offline + deterministic (no real fetch).
const apiMock = vi.hoisted(() => ({
  fetchLlmSelection: vi.fn(),
  fetchLlmModels: vi.fn(),
  pingLm: vi.fn(),
  putLlmBaseUrl: vi.fn(),
  putLlmModel: vi.fn(),
  putLlmProvider: vi.fn(),
}));

vi.mock("../../lib/llmApi", async () => {
  const actual = await vi.importActual<typeof import("../../lib/llmApi")>("../../lib/llmApi");
  return { ...actual, ...apiMock };
});

import { SETUP_COMPLETE_KEY, SetupScreen } from "./SetupScreen";

const MODELS = [
  {
    id: "qwen2.5-7b-instruct",
    quantisation: "Q4_K_M",
    architecture: "qwen2",
    max_context_length: 32768,
    loaded: true,
  },
];

beforeEach(() => {
  window.localStorage.clear();
  apiMock.fetchLlmSelection.mockResolvedValue({
    provider: "lm_studio",
    lm_model: "qwen2.5-7b-instruct",
    context_length: {},
    claude_model: "claude-opus-4",
    base_url: "http://192.168.1.20:1234/v1",
  });
  apiMock.fetchLlmModels.mockResolvedValue(MODELS);
  apiMock.pingLm.mockResolvedValue({ reachable: true, host: "192.168.1.20:1234" });
  apiMock.putLlmBaseUrl.mockResolvedValue({});
  apiMock.putLlmModel.mockResolvedValue({});
  apiMock.putLlmProvider.mockResolvedValue({});
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SetupScreen", () => {
  test("seeds the URL field from the EFFECTIVE selection (not a hardcoded default)", async () => {
    render(<SetupScreen onReady={vi.fn()} />);
    const input = await screen.findByDisplayValue("http://192.168.1.20:1234/v1");
    expect(input).toBeInTheDocument();
  });

  test("loads the model + persists setup_complete + enters the HUD on Démarrer", async () => {
    const onReady = vi.fn();
    render(<SetupScreen onReady={onReady} />);

    // Wait for the reachable ping → model list to surface and Démarrer to enable.
    const start = await screen.findByRole("button", { name: /Démarrer/ });
    await waitFor(() => expect(start).toBeEnabled());

    fireEvent.click(start);

    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    expect(apiMock.putLlmBaseUrl).toHaveBeenCalledWith("http://192.168.1.20:1234/v1");
    expect(apiMock.putLlmModel).toHaveBeenCalledWith("qwen2.5-7b-instruct");
    expect(window.localStorage.getItem(SETUP_COMPLETE_KEY)).toBe("1");
  });

  test("Claude CLI provider needs no model load to start", async () => {
    const onReady = vi.fn();
    render(<SetupScreen onReady={onReady} />);

    fireEvent.click(await screen.findByRole("button", { name: "Claude CLI" }));
    const start = screen.getByRole("button", { name: /Démarrer/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    expect(apiMock.putLlmProvider).toHaveBeenCalledWith("claude_cli");
    expect(apiMock.putLlmModel).not.toHaveBeenCalled();
  });

  test("does not enter the HUD when the model load fails", async () => {
    const onReady = vi.fn();
    apiMock.putLlmModel.mockRejectedValue(new Error("OOM"));
    render(<SetupScreen onReady={onReady} />);

    const start = await screen.findByRole("button", { name: /Démarrer/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    await screen.findByText("OOM");
    expect(onReady).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(SETUP_COMPLETE_KEY)).toBeNull();
  });
});
