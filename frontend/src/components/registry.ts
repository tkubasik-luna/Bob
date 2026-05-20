import type { ComponentType } from "react";
import { ChatMessageBlock } from "./ChatMessageBlock";
import { MarkdownView } from "./MarkdownView";

/**
 * Frontend counterpart to the backend `bob.ui_registry`. Keys MUST match the
 * `component` field emitted by the LLM. Each entry receives a typed `props`
 * bag (validated by the backend schema, so unknown keys are tolerated here).
 */
export type RegistryComponent = ComponentType<{ props: Record<string, unknown> }>;

export const componentRegistry: Record<string, RegistryComponent> = {
  ChatMessage: ChatMessageBlock,
  Markdown: MarkdownView,
};
