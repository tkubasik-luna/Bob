import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The « RÉGLAGES » modal talks to the backend through `lib/llmApi`. We mock that
// module so the test is offline + deterministic — no real fetch. The backend
// already filters embeddings out of `GET /api/llm/models`, so the mocked list
// is chat-only; the test asserts the modal renders exactly the fetched ids and
// highlights the current selection.
//
// These cases are ported from the deleted `components/sphere/ProviderPicker
// .test.tsx` (issue 0089 folds the picker into this modal). The flow gains one
// step: open the top-right gear first, then interact inside the panel.
const apiMock = vi.hoisted(() => ({
  fetchLlmModels: vi.fn(),
  fetchLlmSelection: vi.fn(),
  putLlmModel: vi.fn(),
  putLlmProvider: vi.fn(),
}));

vi.mock("../../lib/llmApi", async () => {
  const actual = await vi.importActual<typeof import("../../lib/llmApi")>("../../lib/llmApi");
  return {
    ...actual,
    fetchLlmModels: apiMock.fetchLlmModels,
    fetchLlmSelection: apiMock.fetchLlmSelection,
    putLlmModel: apiMock.putLlmModel,
    putLlmProvider: apiMock.putLlmProvider,
  };
});

import { LlmModelSwapError } from "../../lib/llmApi";
import { SettingsControl } from "./SettingsControl";

const MODELS = [
  {
    id: "qwen2.5-7b-instruct",
    quantisation: "Q4_K_M",
    architecture: "qwen2",
    max_context_length: 32768,
    loaded: true,
  },
  {
    id: "llama-3.3-70b",
    quantisation: "Q3_K_L",
    architecture: "llama",
    max_context_length: 8192,
    loaded: false,
  },
];

/** Open the gear button → the modal panel. */
function openModal() {
  fireEvent.click(screen.getByRole("button", { name: /réglages/i }));
}

describe("SettingsControl — « RÉGLAGES » modal", () => {
  beforeEach(() => {
    apiMock.fetchLlmModels.mockReset();
    apiMock.fetchLlmSelection.mockReset();
    apiMock.putLlmModel.mockReset();
    apiMock.putLlmProvider.mockReset();
    apiMock.fetchLlmModels.mockResolvedValue(MODELS);
    apiMock.fetchLlmSelection.mockResolvedValue({
      provider: "lm_studio",
      lm_model: "qwen2.5-7b-instruct",
      context_length: { "qwen2.5-7b-instruct": 32768 },
      claude_model: "claude-opus-4",
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  test("renders the gear button and no panel until opened", async () => {
    render(<SettingsControl />);
    // selection loads on mount, but the model list must NOT be fetched yet
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    expect(apiMock.fetchLlmModels).not.toHaveBeenCalled();
    // The panel (dialog) is closed.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  test("fetches the model list on open and renders the live list", async () => {
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());

    openModal();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Opening under LM Studio (the seeded provider) fetches the list once.
    await waitFor(() => expect(apiMock.fetchLlmModels).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveTextContent("qwen2.5-7b-instruct");
    expect(options[1]).toHaveTextContent("llama-3.3-70b");
    // no embedding model leaks in (backend filters them; mocked list is chat-only)
    expect(screen.queryByText(/embed/i)).not.toBeInTheDocument();
  });

  test("highlights the current selection (aria-selected)", async () => {
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    openModal();

    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));

    const selected = screen.getByRole("option", { selected: true });
    expect(selected).toHaveTextContent("qwen2.5-7b-instruct");
    const others = screen.getAllByRole("option", { selected: false });
    expect(others).toHaveLength(1);
    expect(others[0]).toHaveTextContent("llama-3.3-70b");
  });

  test("re-fetches the model list on a fresh open", async () => {
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    openModal(); // open
    await waitFor(() => expect(apiMock.fetchLlmModels).toHaveBeenCalledTimes(1));

    // close (✕) then reopen — the loaded set may have changed server-side, so
    // a fresh open re-fetches.
    fireEvent.click(screen.getByRole("button", { name: /fermer/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    openModal();
    await waitFor(() => expect(apiMock.fetchLlmModels).toHaveBeenCalledTimes(2));
  });

  async function openAndGetRows() {
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    openModal();
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));
  }

  test("clicking a non-current model fires the blocking PUT with a loading state", async () => {
    let resolvePut: (sel: unknown) => void = () => {};
    apiMock.putLlmModel.mockReturnValue(
      new Promise((res) => {
        resolvePut = res;
      }),
    );

    await openAndGetRows();
    const target = screen.getByTestId("set-model-llama-3.3-70b");
    fireEvent.click(target);

    expect(apiMock.putLlmModel).toHaveBeenCalledWith("llama-3.3-70b");
    await waitFor(() => expect(target).toHaveAttribute("aria-busy", "true"));
    expect(target).toBeDisabled();
    expect(target).toHaveTextContent(/chargement/i);

    resolvePut({
      provider: "lm_studio",
      lm_model: "llama-3.3-70b",
      context_length: {},
      claude_model: "claude-opus-4",
    });
    await waitFor(() =>
      expect(screen.getByRole("option", { selected: true })).toHaveTextContent("llama-3.3-70b"),
    );
  });

  test("a failed swap stays on the previous model and shows the error", async () => {
    apiMock.putLlmModel.mockRejectedValue(new LlmModelSwapError("load_failed", "out of memory"));

    await openAndGetRows();
    fireEvent.click(screen.getByTestId("set-model-llama-3.3-70b"));

    await waitFor(() =>
      expect(screen.getByTestId("set-model-llama-3.3-70b")).toHaveTextContent(/out of memory/i),
    );
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("qwen2.5-7b-instruct");
  });

  test("clicking the already-current model does not fire a PUT", async () => {
    await openAndGetRows();
    fireEvent.click(screen.getByTestId("set-model-qwen2.5-7b-instruct"));
    expect(apiMock.putLlmModel).not.toHaveBeenCalled();
  });

  // --- provider switch -------------------------------------------------------

  test("toggling to Claude CLI fires the provider PUT and shows the read-only label", async () => {
    apiMock.putLlmProvider.mockResolvedValue({
      provider: "claude_cli",
      lm_model: "qwen2.5-7b-instruct",
      context_length: {},
      claude_model: "claude-opus-4",
    });

    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    openModal();

    fireEvent.click(screen.getByRole("radio", { name: /claude cli/i }));
    expect(apiMock.putLlmProvider).toHaveBeenCalledWith("claude_cli");

    // The Claude side shows the read-only model label from the backend
    // (`claude_model`), with NO model list.
    await waitFor(() => expect(screen.getByRole("radio", { name: /claude cli/i })).toBeChecked());
    expect(screen.getByText("claude-opus-4")).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    // The "modèle fixe" hint replaces the URL field + model list.
    expect(screen.getByText(/modèle fixe/i)).toBeInTheDocument();
  });

  test("a failed provider switch reverts the toggle and surfaces the error", async () => {
    apiMock.putLlmProvider.mockRejectedValue(
      new LlmModelSwapError("claude_cli_unavailable", "claude binary not found"),
    );

    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    openModal();

    fireEvent.click(screen.getByRole("radio", { name: /claude cli/i }));

    // Reverted to LM Studio (the backend kept the previous provider) + error shown.
    await waitFor(() => expect(screen.getByRole("radio", { name: /lm studio/i })).toBeChecked());
    expect(screen.getByRole("alert")).toHaveTextContent(/claude binary not found/i);
  });

  test("toggling to the already-active provider does not fire a PUT", async () => {
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    openModal();
    // Already on LM Studio (seeded selection).
    fireEvent.click(screen.getByRole("radio", { name: /lm studio/i }));
    expect(apiMock.putLlmProvider).not.toHaveBeenCalled();
  });

  // --- server URL field + presets --------------------------------------------

  test("a preset swaps the URL and flips reachability to connected", async () => {
    await openAndGetRows();
    // localhost preset is a plausible host:port → "connecté".
    fireEvent.click(screen.getByRole("button", { name: "localhost" }));
    const input = screen.getByLabelText(/url du serveur lm studio/i) as HTMLInputElement;
    expect(input.value).toBe("localhost:1234");
    expect(screen.getByText(/serveur joignable/i)).toBeInTheDocument();
  });

  test("an implausible URL shows the offline state", async () => {
    await openAndGetRows();
    const input = screen.getByLabelText(/url du serveur lm studio/i);
    fireEvent.change(input, { target: { value: "x" } });
    expect(screen.getByText(/serveur introuvable/i)).toBeInTheDocument();
  });

  // --- ctx-length slider + Apply (feature 0013) ------------------------------

  test("ctx slider is clamped to the active model's max_context_length", async () => {
    await openAndGetRows();

    const slider = (await screen.findByTestId("set-ctx-slider")) as HTMLInputElement;
    // Active model is qwen with max 32768; the slider's max mirrors it.
    expect(slider.max).toBe("32768");
    expect(Number(slider.min)).toBeLessThanOrEqual(Number(slider.value));
    // Seeded from the persisted per-model ctx (32768 in the seeded selection).
    expect(slider.value).toBe("32768");
  });

  test("dragging the slider updates local state only — no PUT", async () => {
    await openAndGetRows();

    const slider = await screen.findByTestId("set-ctx-slider");
    fireEvent.change(slider, { target: { value: "16384" } });

    expect(screen.getByTestId("set-ctx-value")).toHaveTextContent(/16,?384/);
    expect(apiMock.putLlmModel).not.toHaveBeenCalled();
  });

  test("Apply fires the blocking reload-with-ctx PUT carrying the slider value", async () => {
    let resolvePut: (sel: unknown) => void = () => {};
    apiMock.putLlmModel.mockReturnValue(
      new Promise((res) => {
        resolvePut = res;
      }),
    );

    await openAndGetRows();
    const slider = await screen.findByTestId("set-ctx-slider");
    fireEvent.change(slider, { target: { value: "8192" } });

    const apply = screen.getByTestId("set-ctx-apply");
    fireEvent.click(apply);

    expect(apiMock.putLlmModel).toHaveBeenCalledWith("qwen2.5-7b-instruct", 8192);
    await waitFor(() => expect(apply).toHaveAttribute("aria-busy", "true"));
    expect(apply).toBeDisabled();

    resolvePut({
      provider: "lm_studio",
      lm_model: "qwen2.5-7b-instruct",
      context_length: { "qwen2.5-7b-instruct": 8192 },
      claude_model: "claude-opus-4",
    });
    await waitFor(() => expect(apply).not.toBeDisabled());
  });

  test("a failed ctx Apply surfaces the error and keeps the slider usable", async () => {
    apiMock.putLlmModel.mockRejectedValue(new LlmModelSwapError("load_failed", "out of memory"));

    await openAndGetRows();
    fireEvent.change(await screen.findByTestId("set-ctx-slider"), { target: { value: "8192" } });
    fireEvent.click(screen.getByTestId("set-ctx-apply"));

    await waitFor(() => expect(screen.getByTestId("set-ctx")).toHaveTextContent(/out of memory/i));
    expect(screen.getByTestId("set-ctx-apply")).not.toBeDisabled();
  });
});
