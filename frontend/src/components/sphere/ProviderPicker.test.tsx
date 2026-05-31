import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The picker talks to the backend through `lib/llmApi`. We mock that module so
// the test is offline and deterministic — no real fetch. The backend already
// filters embeddings out of `GET /api/llm/models`, so the mocked list is
// chat-only; the test asserts the picker renders exactly the fetched ids
// (embeddings absent) and highlights the current selection.
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
import { ProviderPicker } from "./ProviderPicker";

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

describe("ProviderPicker", () => {
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

  test("does not fetch models until the dropdown is opened", async () => {
    render(<ProviderPicker />);
    // selection loads on mount, but the model list must NOT be fetched yet
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    expect(apiMock.fetchLlmModels).not.toHaveBeenCalled();
    // list is not in the DOM before open
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  test("fetches the model list on dropdown open and renders the live list", async () => {
    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());

    // open the active-engine row (it is the expandable button)
    const trigger = screen.getByRole("button", { expanded: false });
    fireEvent.click(trigger);

    expect(apiMock.fetchLlmModels).toHaveBeenCalledTimes(1);

    // the two fetched chat models render; nothing else
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveTextContent("qwen2.5-7b-instruct");
    expect(options[1]).toHaveTextContent("llama-3.3-70b");
    // no embedding model leaks in (backend filters them; mocked list is chat-only)
    expect(screen.queryByText(/embed/i)).not.toBeInTheDocument();
  });

  test("highlights the current selection (aria-selected)", async () => {
    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { expanded: false }));

    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));

    const selected = screen.getByRole("option", { selected: true });
    expect(selected).toHaveTextContent("qwen2.5-7b-instruct");
    // the other model is NOT selected
    const others = screen.getAllByRole("option", { selected: false });
    expect(others).toHaveLength(1);
    expect(others[0]).toHaveTextContent("llama-3.3-70b");
  });

  test("only fetches once across open/close/open cycles", async () => {
    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    const trigger = screen.getByRole("button", { expanded: false });
    fireEvent.click(trigger); // open
    await waitFor(() => expect(apiMock.fetchLlmModels).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { expanded: true })); // close
    fireEvent.click(screen.getByRole("button", { expanded: false })); // reopen
    expect(apiMock.fetchLlmModels).toHaveBeenCalledTimes(1);
  });

  async function openAndGetRows() {
    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));
  }

  test("clicking a non-current model fires the blocking PUT with a loading state", async () => {
    // Keep the PUT pending so we can observe the loading state.
    let resolvePut: (sel: unknown) => void = () => {};
    apiMock.putLlmModel.mockReturnValue(
      new Promise((res) => {
        resolvePut = res;
      }),
    );

    await openAndGetRows();
    const target = screen.getByTestId("pv-row-llama-3.3-70b");
    fireEvent.click(target);

    expect(apiMock.putLlmModel).toHaveBeenCalledWith("llama-3.3-70b");
    // Loading state: row is busy + disabled while the swap runs.
    await waitFor(() => expect(target).toHaveAttribute("aria-busy", "true"));
    expect(target).toBeDisabled();
    expect(target).toHaveTextContent(/chargement/i);

    // Finish the swap → success updates the active-engine footer label.
    resolvePut({
      provider: "lm_studio",
      lm_model: "llama-3.3-70b",
      context_length: {},
    });
    await waitFor(() =>
      expect(screen.getByRole("option", { selected: true })).toHaveTextContent("llama-3.3-70b"),
    );
    // Footer label (active-engine row) now shows the new model.
    expect(screen.getByRole("button", { expanded: true })).toHaveTextContent("llama-3.3-70b");
  });

  test("a failed swap stays on the previous model and shows the error", async () => {
    apiMock.putLlmModel.mockRejectedValue(new LlmModelSwapError("load_failed", "out of memory"));

    await openAndGetRows();
    fireEvent.click(screen.getByTestId("pv-row-llama-3.3-70b"));

    // Error surfaced on the row; selection unchanged (still qwen).
    await waitFor(() =>
      expect(screen.getByTestId("pv-row-llama-3.3-70b")).toHaveTextContent(/out of memory/i),
    );
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("qwen2.5-7b-instruct");
    // Footer label still shows the previous model.
    expect(screen.getByRole("button", { expanded: true })).toHaveTextContent("qwen2.5-7b-instruct");
  });

  test("clicking the already-current model does not fire a PUT", async () => {
    await openAndGetRows();
    fireEvent.click(screen.getByTestId("pv-row-qwen2.5-7b-instruct"));
    expect(apiMock.putLlmModel).not.toHaveBeenCalled();
  });

  // --- provider switch (issue 0081) ------------------------------------------

  test("toggling to Claude CLI fires the provider PUT and shows the read-only label", async () => {
    apiMock.putLlmProvider.mockResolvedValue({
      provider: "claude_cli",
      lm_model: "qwen2.5-7b-instruct",
      context_length: {},
      claude_model: "claude-opus-4",
    });

    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: /claude cli/i }));
    expect(apiMock.putLlmProvider).toHaveBeenCalledWith("claude_cli");

    // The Claude side shows the read-only model label from the backend
    // (`claude_model`), with NO model dropdown and NO listbox.
    await waitFor(() => expect(screen.getByRole("radio", { name: /claude cli/i })).toBeChecked());
    expect(screen.getByText("claude-opus-4")).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    // The active-engine row is disabled on the Claude side (no model picker).
    const activeRow = screen.getByRole("button", { name: /CLI bridge/i });
    expect(activeRow).toBeDisabled();
  });

  test("a failed provider switch reverts the toggle and surfaces the error", async () => {
    apiMock.putLlmProvider.mockRejectedValue(
      new LlmModelSwapError("claude_cli_unavailable", "claude binary not found"),
    );

    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: /claude cli/i }));

    // Reverted to LM Studio (the backend kept the previous provider) + error shown.
    await waitFor(() => expect(screen.getByRole("radio", { name: /lm studio/i })).toBeChecked());
    expect(screen.getByRole("alert")).toHaveTextContent(/claude binary not found/i);
  });

  test("toggling to the already-active provider does not fire a PUT", async () => {
    render(<ProviderPicker />);
    await waitFor(() => expect(apiMock.fetchLlmSelection).toHaveBeenCalled());
    // Already on LM Studio (seeded selection).
    fireEvent.click(screen.getByRole("radio", { name: /lm studio/i }));
    expect(apiMock.putLlmProvider).not.toHaveBeenCalled();
  });

  // --- ctx-length slider + Apply (issue 0082) --------------------------------

  test("ctx slider is clamped to the active model's max_context_length", async () => {
    await openAndGetRows();

    const slider = (await screen.findByTestId("pv-ctx-slider")) as HTMLInputElement;
    // Active model is qwen with max 32768; the slider's max mirrors it.
    expect(slider.max).toBe("32768");
    expect(Number(slider.min)).toBeLessThanOrEqual(Number(slider.value));
    // Seeded from the persisted per-model ctx (32768 in the seeded selection).
    expect(slider.value).toBe("32768");
  });

  test("dragging the slider updates local state only — no PUT", async () => {
    await openAndGetRows();

    const slider = await screen.findByTestId("pv-ctx-slider");
    fireEvent.change(slider, { target: { value: "16384" } });

    // The displayed value reflects the drag…
    expect(screen.getByTestId("pv-ctx-value")).toHaveTextContent(/16,?384/);
    // …but NO reload was triggered by the drag alone.
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
    const slider = await screen.findByTestId("pv-ctx-slider");
    fireEvent.change(slider, { target: { value: "8192" } });

    const apply = screen.getByTestId("pv-ctx-apply");
    fireEvent.click(apply);

    // The Apply (not the drag) fires the PUT with the model id + slider ctx.
    expect(apiMock.putLlmModel).toHaveBeenCalledWith("qwen2.5-7b-instruct", 8192);
    // Loading state while in flight.
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
    fireEvent.change(await screen.findByTestId("pv-ctx-slider"), { target: { value: "8192" } });
    fireEvent.click(screen.getByTestId("pv-ctx-apply"));

    await waitFor(() => expect(screen.getByTestId("pv-ctx")).toHaveTextContent(/out of memory/i));
    // Slider re-enabled after the failure so the user can retry.
    expect(screen.getByTestId("pv-ctx-apply")).not.toBeDisabled();
  });
});
