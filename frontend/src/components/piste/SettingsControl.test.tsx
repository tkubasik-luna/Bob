import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// PRD 0016 / issue 0108 — the « RÉGLAGES » modal is now a PER-ROLE picker. It
// talks to the backend through `lib/llmApi`; we mock that module so the test is
// offline + deterministic (no real fetch). These cases mirror the pre-0108
// SettingsControl.test.tsx structure (open the gear, interact inside the panel)
// but target the per-role surface: `GET /api/llm/roles` + `PUT /api/llm/roles/
// {role}`, with the model list from `GET /api/llm/models` feeding each LM Studio
// role's dropdown.
const apiMock = vi.hoisted(() => ({
  fetchLlmRoles: vi.fn(),
  putLlmRole: vi.fn(),
  fetchLlmModels: vi.fn(),
  pingLm: vi.fn(),
}));

vi.mock("../../lib/llmApi", async () => {
  const actual = await vi.importActual<typeof import("../../lib/llmApi")>("../../lib/llmApi");
  return {
    ...actual,
    fetchLlmRoles: apiMock.fetchLlmRoles,
    putLlmRole: apiMock.putLlmRole,
    fetchLlmModels: apiMock.fetchLlmModels,
    pingLm: apiMock.pingLm,
  };
});

import { LlmRoleSwapError, type RoleMap } from "../../lib/llmApi";
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
    id: "qwen2.5-3b-instruct",
    quantisation: "Q4_K_M",
    architecture: "qwen2",
    max_context_length: 16384,
    loaded: false,
  },
];

/** A complete per-role map: jarvis/thinker/draft on LM Studio, subagent on
 * Claude CLI — exercising both provider sides + the budget/stt blocks. */
function baseRoleMap(): RoleMap {
  return {
    schema_version: 2,
    roles: {
      jarvis: {
        provider: "lm_studio",
        base_url: "http://localhost:1234/v1",
        lm_model: "qwen2.5-7b-instruct",
        context_length: { "qwen2.5-7b-instruct": 32768 },
      },
      thinker: {
        provider: "lm_studio",
        base_url: "http://localhost:1234/v1",
        lm_model: "qwen2.5-3b-instruct",
        context_length: {},
      },
      draft: {
        provider: "lm_studio",
        base_url: "http://localhost:1234/v1",
        lm_model: "qwen2.5-3b-instruct",
        context_length: {},
      },
      subagent: {
        provider: "claude_cli",
        base_url: null,
        lm_model: null,
        context_length: {},
      },
    },
    stt: { engine: "whisper_cpp", model: "large-v3-turbo" },
    budget: { ceiling_gib: null, reserve_gib: 8, per_host_override: {} },
    claude_model: "claude-opus-4",
  };
}

/** Open the gear button → the modal panel. */
function openModal() {
  fireEvent.click(screen.getByRole("button", { name: /réglages/i }));
}

async function openPanel() {
  render(<SettingsControl />);
  await waitFor(() => expect(apiMock.fetchLlmRoles).toHaveBeenCalled());
  openModal();
  expect(screen.getByRole("dialog")).toBeInTheDocument();
}

describe("SettingsControl — per-role « RÉGLAGES » modal", () => {
  beforeEach(() => {
    apiMock.fetchLlmRoles.mockReset();
    apiMock.putLlmRole.mockReset();
    apiMock.fetchLlmModels.mockReset();
    apiMock.pingLm.mockReset();
    apiMock.fetchLlmRoles.mockResolvedValue(baseRoleMap());
    apiMock.fetchLlmModels.mockResolvedValue(MODELS);
    apiMock.pingLm.mockResolvedValue({ reachable: true, host: "localhost:1234" });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  test("renders the gear button and no panel until opened", async () => {
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmRoles).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  // --- per-role render -------------------------------------------------------

  test("renders one block per role (jarvis/thinker/draft/subagent)", async () => {
    await openPanel();
    for (const role of ["jarvis", "thinker", "draft", "subagent"]) {
      expect(screen.getByTestId(`set-role-${role}`)).toBeInTheDocument();
    }
  });

  test("an LM Studio role shows its model dropdown; the Claude role shows the CLI hint", async () => {
    await openPanel();
    // jarvis is LM Studio → dropdown present, seeded to its pinned model.
    await waitFor(() =>
      expect((screen.getByTestId("set-role-model-jarvis") as HTMLSelectElement).value).toBe(
        "qwen2.5-7b-instruct",
      ),
    );
    // subagent is Claude CLI → no dropdown, read-only model label in the hint.
    expect(screen.queryByTestId("set-role-model-subagent")).not.toBeInTheDocument();
    const sub = screen.getByTestId("set-role-subagent");
    expect(within(sub).getByText(/claude-opus-4/)).toBeInTheDocument();
  });

  test("the model dropdown is fed by the role's host GET /models", async () => {
    await openPanel();
    const select = (await screen.findByTestId("set-role-model-jarvis")) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("qwen2.5-7b-instruct");
    expect(optionValues).toContain("qwen2.5-3b-instruct");
    expect(apiMock.fetchLlmModels).toHaveBeenCalled();
  });

  // --- provider selection (per role) -----------------------------------------

  test("switching a role to Claude CLI fires PUT /roles/{role} with the Claude shape", async () => {
    const next = baseRoleMap();
    next.roles.jarvis = {
      provider: "claude_cli",
      base_url: null,
      lm_model: null,
      context_length: {},
    };
    apiMock.putLlmRole.mockResolvedValue(next);

    await openPanel();
    const jarvis = screen.getByTestId("set-role-jarvis");
    fireEvent.click(within(jarvis).getByRole("radio", { name: /claude cli/i }));

    expect(apiMock.putLlmRole).toHaveBeenCalledWith("jarvis", {
      provider: "claude_cli",
      base_url: null,
      lm_model: null,
      context_length: {},
    });
    // After the map adopts, jarvis shows the Claude side (radio checked, no dropdown).
    await waitFor(() =>
      expect(within(jarvis).getByRole("radio", { name: /claude cli/i })).toBeChecked(),
    );
    expect(screen.queryByTestId("set-role-model-jarvis")).not.toBeInTheDocument();
  });

  test("toggling a role to its already-active provider does not fire a PUT", async () => {
    await openPanel();
    const jarvis = screen.getByTestId("set-role-jarvis");
    // jarvis is already LM Studio.
    fireEvent.click(within(jarvis).getByRole("radio", { name: /lm studio/i }));
    expect(apiMock.putLlmRole).not.toHaveBeenCalled();
  });

  // --- model selection (per role) --------------------------------------------

  test("selecting a different model fires PUT /roles/{role} carrying the new model", async () => {
    apiMock.putLlmRole.mockResolvedValue(baseRoleMap());
    await openPanel();
    const select = (await screen.findByTestId("set-role-model-jarvis")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "qwen2.5-3b-instruct" } });

    expect(apiMock.putLlmRole).toHaveBeenCalledWith("jarvis", {
      provider: "lm_studio",
      base_url: "http://localhost:1234/v1",
      lm_model: "qwen2.5-3b-instruct",
      context_length: { "qwen2.5-7b-instruct": 32768 },
    });
  });

  test("the swap shows a loading state then adopts the returned map", async () => {
    let resolvePut: (m: RoleMap) => void = () => {};
    apiMock.putLlmRole.mockReturnValue(
      new Promise<RoleMap>((res) => {
        resolvePut = res;
      }),
    );
    await openPanel();
    const select = (await screen.findByTestId("set-role-model-jarvis")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "qwen2.5-3b-instruct" } });

    // loading row visible + the select disabled while in flight.
    await waitFor(() => expect(select).toBeDisabled());

    const next = baseRoleMap();
    next.roles.jarvis.lm_model = "qwen2.5-3b-instruct";
    resolvePut(next);
    await waitFor(() => expect(select).not.toBeDisabled());
    await waitFor(() => expect(select.value).toBe("qwen2.5-3b-instruct"));
  });

  // --- ctx slider (per role) -------------------------------------------------

  test("dragging a role's ctx slider updates local state only — no PUT", async () => {
    await openPanel();
    const slider = await screen.findByTestId("set-role-ctx-slider-jarvis");
    fireEvent.change(slider, { target: { value: "16384" } });
    expect(apiMock.putLlmRole).not.toHaveBeenCalled();
  });

  test("Apply on a role's ctx fires PUT with the per-model context_length", async () => {
    apiMock.putLlmRole.mockResolvedValue(baseRoleMap());
    await openPanel();
    const slider = await screen.findByTestId("set-role-ctx-slider-jarvis");
    fireEvent.change(slider, { target: { value: "8192" } });
    fireEvent.click(screen.getByTestId("set-role-ctx-apply-jarvis"));

    expect(apiMock.putLlmRole).toHaveBeenCalledWith("jarvis", {
      provider: "lm_studio",
      base_url: "http://localhost:1234/v1",
      lm_model: "qwen2.5-7b-instruct",
      context_length: { "qwen2.5-7b-instruct": 8192 },
    });
  });

  // --- per-role badges (ready / offline) -------------------------------------

  test("the Claude role badge is always ready; an unreachable LM role goes offline", async () => {
    // Make the LM host unreachable so the ping resolves offline.
    apiMock.pingLm.mockResolvedValue({ reachable: false, host: "" });
    await openPanel();

    // Claude (subagent) is ready regardless of any ping.
    expect(screen.getByTestId("set-role-badge-subagent")).toHaveAttribute("data-state", "ready");
    // jarvis (LM Studio) flips to offline once the debounced ping resolves.
    await waitFor(() =>
      expect(screen.getByTestId("set-role-badge-jarvis")).toHaveAttribute("data-state", "offline"),
    );
  });

  test("a reachable LM role shows a ready badge", async () => {
    await openPanel();
    await waitFor(() =>
      expect(screen.getByTestId("set-role-badge-jarvis")).toHaveAttribute("data-state", "ready"),
    );
  });

  // --- budget over-budget warning --------------------------------------------

  test("a budget_exceeded swap surfaces the over-budget warning on the role", async () => {
    apiMock.putLlmRole.mockRejectedValue(
      new LlmRoleSwapError(
        "budget_exceeded",
        "chargement refusé : dépasse le plafond mémoire — libère un rôle pour ce host.",
      ),
    );
    await openPanel();
    const select = (await screen.findByTestId("set-role-model-jarvis")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "qwen2.5-3b-instruct" } });

    const err = await screen.findByTestId("set-role-error-jarvis");
    expect(err).toHaveTextContent(/dépasse le plafond/i);
    expect(err).toHaveTextContent(/budget/i);
    // The role stays on its previous model (we never adopted a new map).
    expect(select.value).toBe("qwen2.5-7b-instruct");
  });

  test("the budget section renders the ceiling + reserve config", async () => {
    await openPanel();
    expect(screen.getByTestId("budget-section")).toBeInTheDocument();
    // ceiling_gib null → "détection auto"; reserve 8 Gio.
    expect(screen.getByTestId("budget-ceiling")).toHaveTextContent(/détection auto/i);
    expect(screen.getByTestId("budget-reserve")).toHaveTextContent(/8/);
  });

  test("a pinned ceiling + per-host override render their values", async () => {
    const map = baseRoleMap();
    map.budget = {
      ceiling_gib: 48,
      reserve_gib: 8,
      per_host_override: { "192.168.1.20:1234": 64 },
    };
    apiMock.fetchLlmRoles.mockResolvedValue(map);
    await openPanel();
    expect(screen.getByTestId("budget-ceiling")).toHaveTextContent(/48/);
    expect(screen.getByTestId("budget-override-192.168.1.20:1234")).toHaveTextContent(/64/);
  });

  // --- STT section -----------------------------------------------------------

  test("the STT section shows the whisper.cpp engine + the model", async () => {
    await openPanel();
    expect(screen.getByTestId("stt-block")).toHaveTextContent(/whisper\.cpp/i);
    expect((screen.getByTestId("stt-model") as HTMLInputElement).value).toBe("large-v3-turbo");
  });

  // --- degraded path ---------------------------------------------------------

  test("a failed GET /roles still renders the panel with a degraded notice", async () => {
    apiMock.fetchLlmRoles.mockRejectedValue(new Error("network"));
    render(<SettingsControl />);
    await waitFor(() => expect(apiMock.fetchLlmRoles).toHaveBeenCalled());
    openModal();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/sélection par rôle indisponible/i)).toBeInTheDocument();
  });

  // --- VOIX toggle (unchanged behaviour) -------------------------------------

  test("clicking the VOIX toggle flips voice state + glyph", async () => {
    await openPanel();
    const toggle = screen.getByTestId("set-voice-toggle");
    const before = toggle.getAttribute("aria-pressed");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).not.toBe(before);
    const onNow = toggle.getAttribute("aria-pressed") === "true";
    expect(screen.queryByTestId(onNow ? "speaker-on-icon" : "speaker-off-icon")).not.toBeNull();
  });

  test("global `M` toggles voice, but not while typing in an input", async () => {
    await openPanel();
    const toggle = screen.getByTestId("set-voice-toggle");
    const before = toggle.getAttribute("aria-pressed");
    fireEvent.keyDown(window, { key: "m" });
    const afterM = toggle.getAttribute("aria-pressed");
    expect(afterM).not.toBe(before);

    // typing "m" in a role URL field must NOT toggle (input focused).
    const input = screen.getByLabelText(/url du serveur · jarvis/i);
    input.focus();
    fireEvent.keyDown(input, { key: "m" });
    expect(toggle.getAttribute("aria-pressed")).toBe(afterM);
  });
});
