import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The SetupScreen talks to the backend through `lib/llmApi`; mock it so the
// test is offline + deterministic (no real fetch).
const apiMock = vi.hoisted(() => ({
  fetchLlmRoles: vi.fn(),
  fetchLlmModels: vi.fn(),
  pingLm: vi.fn(),
  putLlmRole: vi.fn(),
}));

vi.mock("../../lib/llmApi", async () => {
  const actual = await vi.importActual<typeof import("../../lib/llmApi")>("../../lib/llmApi");
  return { ...actual, ...apiMock };
});

import { SETUP_COMPLETE_KEY, SetupScreen, normaliseBaseUrl } from "./SetupScreen";

describe("normaliseBaseUrl", () => {
  test("adds the scheme + /v1 path and collapses loopback aliases to localhost", () => {
    // The raw URL feeds the INFERENCE client while ping/models canonicalise
    // server-side — "127.0.0.0:1234" pinged green yet broke inference.
    expect(normaliseBaseUrl("127.0.0.0:1234")).toBe("http://localhost:1234/v1");
    expect(normaliseBaseUrl("127.0.0.1:1234")).toBe("http://localhost:1234/v1");
    expect(normaliseBaseUrl("0.0.0.0:1234")).toBe("http://localhost:1234/v1");
    expect(normaliseBaseUrl("localhost:1234")).toBe("http://localhost:1234/v1");
    expect(normaliseBaseUrl("  http://localhost:1234/v1  ")).toBe("http://localhost:1234/v1");
    // Remote hosts + explicit paths are preserved.
    expect(normaliseBaseUrl("http://192.168.1.20:1234/v1")).toBe("http://192.168.1.20:1234/v1");
    expect(normaliseBaseUrl("192.168.1.20:1234")).toBe("http://192.168.1.20:1234/v1");
    expect(normaliseBaseUrl("")).toBe("");
  });
});

const MODELS = [
  {
    id: "qwen2.5-7b-instruct",
    quantisation: "Q4_K_M",
    architecture: "qwen2",
    max_context_length: 32768,
    loaded: true,
  },
  {
    id: "llama-3.1-8b",
    quantisation: "Q4_K_M",
    architecture: "llama",
    max_context_length: 131072,
    loaded: false,
  },
];

const lmRole = (model: string) => ({
  provider: "lm_studio",
  base_url: "http://192.168.1.20:1234/v1",
  lm_model: model,
  context_length: { [model]: 8192 },
  reasoning: null,
});

const claudeRole = () => ({
  provider: "claude_cli",
  base_url: null,
  lm_model: null,
  context_length: {},
  reasoning: null,
});

const ROLE_MAP = {
  schema_version: 2,
  roles: {
    jarvis: lmRole("qwen2.5-7b-instruct"),
    thinker: claudeRole(),
    draft: lmRole("llama-3.1-8b"),
    subagent: lmRole("qwen2.5-7b-instruct"),
  },
  stt: { engine: "sherpa", model: "parakeet" },
  budget: { ceiling_gib: null, reserve_gib: 4, per_host_override: {} },
  claude_model: "claude-opus-4",
};

beforeEach(() => {
  window.localStorage.clear();
  apiMock.fetchLlmRoles.mockResolvedValue(ROLE_MAP);
  apiMock.fetchLlmModels.mockResolvedValue(MODELS);
  apiMock.pingLm.mockResolvedValue({ reachable: true, host: "192.168.1.20:1234" });
  apiMock.putLlmRole.mockResolvedValue(ROLE_MAP);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SetupScreen", () => {
  test("seeds the URL field + per-role picks from the EFFECTIVE role map", async () => {
    render(<SetupScreen onReady={vi.fn()} />);
    const input = await screen.findByDisplayValue("http://192.168.1.20:1234/v1");
    expect(input).toBeInTheDocument();

    // Each role's select reflects its committed selection.
    const jarvis = (await screen.findByLabelText("Modèle Jarvis · voix")) as HTMLSelectElement;
    await waitFor(() => expect(jarvis.value).toBe("lm:qwen2.5-7b-instruct"));
    const thinker = screen.getByLabelText("Modèle Penseur") as HTMLSelectElement;
    expect(thinker.value).toBe("claude");
    const draft = screen.getByLabelText("Modèle Brouillon") as HTMLSelectElement;
    expect(draft.value).toBe("lm:llama-3.1-8b");
  });

  test("commits every role sequentially + shows the loading board + enters the HUD", async () => {
    const onReady = vi.fn();
    render(<SetupScreen onReady={onReady} />);

    const start = await screen.findByRole("button", { name: "Démarrer" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    // The loading board appears with one row per role.
    const board = await screen.findByTestId("setup-loading");
    expect(within(board).getByText("Jarvis · voix")).toBeInTheDocument();
    expect(within(board).getByText("Penseur")).toBeInTheDocument();

    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    expect(apiMock.putLlmRole).toHaveBeenCalledTimes(4);
    expect(apiMock.putLlmRole).toHaveBeenNthCalledWith(1, "jarvis", {
      provider: "lm_studio",
      base_url: "http://192.168.1.20:1234/v1",
      lm_model: "qwen2.5-7b-instruct",
      context_length: { "qwen2.5-7b-instruct": 8192 },
      reasoning: null,
    });
    expect(apiMock.putLlmRole).toHaveBeenNthCalledWith(2, "thinker", {
      provider: "claude_cli",
      base_url: null,
      lm_model: null,
      context_length: {},
      reasoning: null,
    });
    expect(window.localStorage.getItem(SETUP_COMPLETE_KEY)).toBe("1");
  });

  test("a Claude pick for every role needs no LM Studio server", async () => {
    apiMock.fetchLlmRoles.mockResolvedValue({
      ...ROLE_MAP,
      roles: {
        jarvis: claudeRole(),
        thinker: claudeRole(),
        draft: claudeRole(),
        subagent: claudeRole(),
      },
    });
    const onReady = vi.fn();
    render(<SetupScreen onReady={onReady} />);

    // No base_url anywhere → the URL field stays empty, no ping/models fetch.
    const start = await screen.findByRole("button", { name: "Démarrer" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    expect(apiMock.putLlmRole).toHaveBeenCalledTimes(4);
    expect(apiMock.fetchLlmModels).not.toHaveBeenCalled();
    for (const call of apiMock.putLlmRole.mock.calls) {
      expect(call[1]).toMatchObject({ provider: "claude_cli" });
    }
  });

  test("a late seed does not clobber a URL the user already typed", async () => {
    // Race: fetchLlmRoles resolves AFTER the user edits the URL. The stale
    // stored URL must NOT overwrite the typed one (which made the ping keep
    // probing the old server).
    let resolveSeed!: (map: unknown) => void;
    apiMock.fetchLlmRoles.mockReturnValue(
      new Promise((res) => {
        resolveSeed = res;
      }),
    );
    render(<SetupScreen onReady={vi.fn()} />);

    const input = await screen.findByPlaceholderText("http://localhost:1234/v1");
    fireEvent.change(input, { target: { value: "http://192.168.4.94:1234/v1" } });

    resolveSeed(ROLE_MAP);

    // The user's typed URL survives.
    await waitFor(() =>
      expect((input as HTMLInputElement).value).toBe("http://192.168.4.94:1234/v1"),
    );
  });

  test("stops on the failing role + offers a way back, without entering the HUD", async () => {
    const onReady = vi.fn();
    apiMock.putLlmRole.mockImplementation(async (role: string) => {
      if (role === "draft") throw new Error("OOM");
      return ROLE_MAP;
    });
    render(<SetupScreen onReady={onReady} />);

    const start = await screen.findByRole("button", { name: "Démarrer" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    await screen.findByText("OOM");
    expect(onReady).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(SETUP_COMPLETE_KEY)).toBeNull();
    // jarvis + thinker committed, draft failed, subagent never attempted.
    expect(apiMock.putLlmRole).toHaveBeenCalledTimes(3);

    // The board offers a way back to the form.
    fireEvent.click(screen.getByRole("button", { name: "Retour à la configuration" }));
    await screen.findByTestId("setup-roles");
  });
});
