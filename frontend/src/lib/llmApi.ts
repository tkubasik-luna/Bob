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

/** Current selection — mirror of the backend `LLMSelectionResponse`. */
export type LlmSelection = {
  provider: string;
  lm_model: string | null;
  context_length: Record<string, number>;
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
