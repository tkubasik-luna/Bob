// Thin typed client for the read-only LLM REST endpoints (PRD 0012).
//   GET /api/llm/selection — current provider + pinned LM Studio model (0078)
//   GET /api/llm/models    — live list of chat-capable LM Studio models (0079)
// Both are read-only at this stage: no select/load/PUT. The ProviderPicker
// calls `fetchLlmModels` ON DROPDOWN OPEN and `fetchLlmSelection` once to
// highlight the current selection.

import { API_BASE_URL } from "../config";

/** One chat-capable LM Studio model — mirror of the backend `LLMModel`
 * (`bob.llm_router`). Embeddings are filtered out server-side; this list is
 * already chat-only. */
export type LlmModel = {
  id: string;
  quantisation: string | null;
  architecture: string | null;
  max_context_length: number | null;
  loaded: boolean;
};

/** Current selection — mirror of the backend `LLMSelectionResponse`.
 *
 * `claude_model` (issue 0081) is the READ-ONLY model label shown on the Claude
 * CLI side of the picker (from `CLAUDE_CLI_MODEL`, or a server default). There
 * is no Claude model dropdown — it is informational only. */
export type LlmSelection = {
  provider: string;
  lm_model: string | null;
  context_length: Record<string, number>;
  claude_model: string;
  /** Active LM Studio inference base URL (e.g. `http://192.168.1.20:1234/v1`).
   * `null` falls back to the server's `.env` `LLM_BASE_URL`. Runtime-swappable
   * via {@link putLlmBaseUrl}. */
  base_url: string | null;
};

/** Raised by {@link fetchLlmModels} when LM Studio is unreachable (the backend
 * returns 503 with a structured body). The caller renders a degraded "serveur
 * injoignable" state rather than crashing the picker. */
export class LlmModelsUnavailableError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "LlmModelsUnavailableError";
  }
}

/** Fetch the live list of chat-capable LM Studio models.
 *
 * On the backend's 503 (LM Studio down) this throws
 * {@link LlmModelsUnavailableError} so the picker can show an explicit error
 * row instead of an empty/loading list forever. */
export async function fetchLlmModels(signal?: AbortSignal): Promise<LlmModel[]> {
  const res = await fetch(`${API_BASE_URL}/api/llm/models`, { signal });
  if (res.status === 503) {
    let detail = "LM Studio injoignable";
    try {
      const body = (await res.json()) as { detail?: string };
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // keep the default detail
    }
    throw new LlmModelsUnavailableError(detail);
  }
  if (!res.ok) {
    throw new Error(`GET /api/llm/models failed: ${res.status}`);
  }
  const body = (await res.json()) as { models: LlmModel[] };
  return body.models;
}

/** Fetch the current LLM selection (provider + pinned LM Studio model). */
export async function fetchLlmSelection(signal?: AbortSignal): Promise<LlmSelection> {
  const res = await fetch(`${API_BASE_URL}/api/llm/selection`, { signal });
  if (!res.ok) {
    throw new Error(`GET /api/llm/selection failed: ${res.status}`);
  }
  return (await res.json()) as LlmSelection;
}

/** Result of {@link pingLm} — a real LM Studio reachability probe. */
export type LlmPing = { reachable: boolean; host: string };

/** Probe whether an LM Studio server is reachable (a real online ping).
 *
 * With `baseUrl`, probes that CANDIDATE server (used to validate a typed/preset
 * URL before committing it); without it, probes the currently-configured one.
 * Always resolves (never throws on an unreachable server — the backend returns
 * 200 `{reachable:false}`); a transport/HTTP error collapses to
 * `{reachable:false}` so the caller can render an offline chip unconditionally. */
export async function pingLm(baseUrl?: string, signal?: AbortSignal): Promise<LlmPing> {
  const qs = baseUrl ? `?base_url=${encodeURIComponent(baseUrl)}` : "";
  try {
    const res = await fetch(`${API_BASE_URL}/api/llm/ping${qs}`, { signal });
    if (!res.ok) return { reachable: false, host: baseUrl ?? "" };
    return (await res.json()) as LlmPing;
  } catch {
    return { reachable: false, host: baseUrl ?? "" };
  }
}

/** Raised by {@link putLlmModel} when the blocking model swap fails (the backend
 * returns 404 / 409 / 503 with a structured `{error, detail}` body). The picker
 * stays on the PREVIOUS model and surfaces `message` as the error detail. */
export class LlmModelSwapError extends Error {
  readonly code: string;
  constructor(code: string, detail: string) {
    super(detail);
    this.name = "LlmModelSwapError";
    this.code = code;
  }
}

/** Change the active LM Studio model — synchronous + BLOCKING (PRD 0012 / 0080).
 *
 * The backend validate-then-swaps: it loads the target model, unloads the
 * previous one, swaps the LM client for both orchestrator roles, then persists
 * the JSON. This can take a while (model load), so callers show a loading state
 * while it is in flight. The generous timeout lives server-side.
 *
 * `contextLength` (issue 0082) is the optional ctx-slider Apply value: when
 * given the model is loaded AT that window, the value is pinned per-model in the
 * selection JSON, and the bounded-context token budget couples to it. Omitting
 * it reuses any persisted per-model ctx, else the model default.
 *
 * On failure the backend keeps the previous selection and returns a structured
 * error body; we throw {@link LlmModelSwapError} so the caller stays on the
 * previous model and shows the detail. On success the returned
 * {@link LlmSelection} reflects the new pinned model. */
export async function putLlmModel(
  lmModel: string,
  contextLength?: number,
  signal?: AbortSignal,
): Promise<LlmSelection> {
  const body: { lm_model: string; context_length?: number } = { lm_model: lmModel };
  if (contextLength !== undefined) body.context_length = contextLength;
  const res = await fetch(`${API_BASE_URL}/api/llm/selection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let code = "swap_failed";
    let detail = `PUT /api/llm/selection failed: ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string; detail?: string };
      if (typeof body.error === "string" && body.error) code = body.error;
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // keep the defaults
    }
    throw new LlmModelSwapError(code, detail);
  }
  return (await res.json()) as LlmSelection;
}

/** Switch the active provider — Claude CLI ↔ LM Studio (PRD 0012 / issue 0081).
 *
 * Synchronous + BLOCKING, mirroring {@link putLlmModel}: the backend validates
 * the target first (LM Studio reachable / `claude` binary on PATH), then
 * rebuilds + swaps the client for both orchestrator roles and persists the
 * JSON. On a validation failure the backend keeps the PREVIOUS provider and
 * returns a structured `{error, detail}` body; we throw {@link LlmModelSwapError}
 * so the caller can revert the toggle and surface the detail. On success the
 * returned {@link LlmSelection} reflects the new active provider. */
export async function putLlmProvider(
  provider: string,
  signal?: AbortSignal,
): Promise<LlmSelection> {
  const res = await fetch(`${API_BASE_URL}/api/llm/selection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider }),
    signal,
  });
  if (!res.ok) {
    let code = "swap_failed";
    let detail = `PUT /api/llm/selection failed: ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string; detail?: string };
      if (typeof body.error === "string" && body.error) code = body.error;
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // keep the defaults
    }
    throw new LlmModelSwapError(code, detail);
  }
  return (await res.json()) as LlmSelection;
}

/** Switch the active LM Studio inference base URL — synchronous + BLOCKING.
 *
 * The backend probes the target server FIRST: an unreachable URL keeps the
 * previous one and returns 503, so we throw {@link LlmModelSwapError} and the
 * caller reverts the field. On success the new URL drives both the inference
 * client and the management SDK host, is persisted, and the returned
 * {@link LlmSelection} reflects it. */
export async function putLlmBaseUrl(baseUrl: string, signal?: AbortSignal): Promise<LlmSelection> {
  const res = await fetch(`${API_BASE_URL}/api/llm/selection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_url: baseUrl }),
    signal,
  });
  if (!res.ok) {
    let code = "swap_failed";
    let detail = `PUT /api/llm/selection failed: ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string; detail?: string };
      if (typeof body.error === "string" && body.error) code = body.error;
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // keep the defaults
    }
    throw new LlmModelSwapError(code, detail);
  }
  return (await res.json()) as LlmSelection;
}

// =============================================================================
// Per-role selection — PRD 0016 / issue 0106 + 0108 (the per-role RolePicker)
// =============================================================================
//
// The v2 surface sits ALONGSIDE the global `/selection` endpoints above:
//   GET  /api/llm/roles        → the full per-role map (+ stt + budget).
//   PUT  /api/llm/roles/{role} → swap ONE role's selection; rebuilds only that
//                                role's client (the other three are untouched).
// Mirror of `bob.llm_router.RoleMapResponse`. The four role ids are the fixed
// `bob.llm_selection_store.ROLES` vocabulary.

/** The four orchestrator roles (mirror of the backend `ROLES` tuple). The
 * RolePicker renders one block per role in this order. */
export const LLM_ROLES = ["jarvis", "thinker", "draft", "subagent"] as const;
export type LlmRole = (typeof LLM_ROLES)[number];

/** One role's flat selection — mirror of the backend `RoleSelectionBody`.
 *
 * `provider` is `lm_studio` | `claude_cli`; `base_url` / `lm_model` are
 * per-role (a role may pin its own server + model); `context_length` is the
 * per-model ctx map round-tripped for budgeting. A Claude-CLI role carries a
 * null `base_url` / `lm_model` (the model label is the shared `claude_model`). */
export type RoleSelection = {
  provider: string;
  base_url: string | null;
  lm_model: string | null;
  context_length: Record<string, number>;
};

/** The `stt` block — mirror of the backend `SttSelectionBody` (Annexe D). */
export type SttSelection = {
  engine: string;
  model: string;
};

/** The `budget` block — mirror of the backend `BudgetSelectionBody` (Annexe D).
 *
 * This is the model-budget CONFIG, not live usage. `ceiling_gib` is the global
 * per-host RAM ceiling in GiB (`null` = "detect later" — the local-RAM probe);
 * `reserve_gib` is the OS head-room margin; `per_host_override` maps a host
 * (`host:port` or base URL) to an explicit ceiling for remote servers.
 *
 * NOTE (seam, issue 0107): the backend exposes no LIVE resident-usage endpoint —
 * `model_budget.HostBudget.resident_gib()` lives only in-process on the
 * `LMStudioManager`. The picker renders this config + surfaces an over-budget
 * REFUSAL via the `PUT` 409 `budget_exceeded` error; it cannot show a live
 * usage/ceiling gauge until a usage endpoint ships. */
export type BudgetSelection = {
  ceiling_gib: number | null;
  reserve_gib: number;
  per_host_override: Record<string, number>;
};

/** The full per-role map — mirror of the backend `RoleMapResponse`.
 *
 * `claude_model` is the read-only Claude label (mirrors `GET /selection`) so a
 * per-role Claude pick renders a model name without a separate fetch. */
export type RoleMap = {
  schema_version: number;
  roles: Record<string, RoleSelection>;
  stt: SttSelection;
  budget: BudgetSelection;
  claude_model: string;
};

/** Raised by {@link putLlmRole} when a per-role swap fails (the backend returns
 * 404 / 409 / 422 / 503 with a structured `{error, detail}` body). The picker
 * keeps the PREVIOUS per-role selection and surfaces `message` as the detail.
 *
 * `code === "budget_exceeded"` is the Annexe G over-budget refusal (409): the
 * role's model would push the host's resident set over its ceiling, so the load
 * was refused BEFORE touching the SDK — the picker shows the over-budget
 * warning. Other codes: `unknown_role` (404), `model_not_found` (404),
 * `load_failed` (409 — real OOM), `unknown_provider` (422),
 * `lm_studio_unavailable` (503), `swap_unavailable` (503 — lifespan not up). */
export class LlmRoleSwapError extends Error {
  readonly code: string;
  constructor(code: string, detail: string) {
    super(detail);
    this.name = "LlmRoleSwapError";
    this.code = code;
  }
}

/** Fetch the full per-role LLM selection map (+ stt + budget). Read-only. */
export async function fetchLlmRoles(signal?: AbortSignal): Promise<RoleMap> {
  const res = await fetch(`${API_BASE_URL}/api/llm/roles`, { signal });
  if (!res.ok) {
    throw new Error(`GET /api/llm/roles failed: ${res.status}`);
  }
  return (await res.json()) as RoleMap;
}

/** Swap ONE role's selection — synchronous + BLOCKING (issue 0106).
 *
 * The backend validates the role id + provider, rebuilds ONLY that role's
 * client (the per-host multi-load budget check can refuse here), persists the
 * v2 map, and returns the full updated {@link RoleMap}. On failure it keeps the
 * previous per-role state and returns a structured `{error, detail}` body; we
 * throw {@link LlmRoleSwapError} so the caller stays on the previous selection
 * and surfaces the detail (e.g. the `budget_exceeded` over-budget warning). */
export async function putLlmRole(
  role: LlmRole,
  selection: RoleSelection,
  signal?: AbortSignal,
): Promise<RoleMap> {
  const res = await fetch(`${API_BASE_URL}/api/llm/roles/${role}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(selection),
    signal,
  });
  if (!res.ok) {
    let code = "swap_failed";
    let detail = `PUT /api/llm/roles/${role} failed: ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string; detail?: string };
      if (typeof body.error === "string" && body.error) code = body.error;
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // keep the defaults
    }
    throw new LlmRoleSwapError(code, detail);
  }
  return (await res.json()) as RoleMap;
}
