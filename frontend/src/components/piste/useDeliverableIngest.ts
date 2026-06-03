// useDeliverableIngest.ts — bridge the real event sources into the
// `deliverableStore` (PRD 0014 / issue 0087).
//
// Keeps ALL ingestion logic out of `DataSlot` / `DataDock` so those stay thin
// renderers. Subscribes to `chatStore` and pushes each generated deliverable
// into `deliverableStore.add` (which dedupes by source id, so re-running on
// every store change — including the connect-time `task_*` replay — is safe and
// idempotent).
//
// Two sources, exactly as the issue specifies:
//   1. SUB-TASK RESULTS — `chatStore.tasks[id].resultPayload` (the structured
//      deliverable a sub-agent resolved to). Keyed by the task id; the card's
//      title/goal come straight from the Task.
//   2. BOB'S UI_PAYLOAD — Bob streams a SINGLE descriptor; it lands as
//      `streamingAssistant.ui` while in flight and on the persisted
//      `messages[].ui` (a list) once the turn closes. Wrapped into a list-of-one
//      and keyed by the `msg_id` (the persisted message id) so the in-flight and
//      final copies dedupe to ONE card.

import { useEffect } from "react";
import type { DeliverableCardTask } from "../../lib/deliverableCard";
import { useChatStore } from "../../store/chatStore";
import { useDeliverableStore } from "../../store/deliverableStore";
import type { ChatMessage, ComponentDescriptor, Task } from "../../types/ws";

/** Trim a free-form text to a compact, single-line card title. Bob's messages
 * carry no title field, so we derive one from the first non-empty line of the
 * spoken/markdown content, capped so the card head doesn't overflow. */
function deriveTitle(text: string): string {
  const firstLine =
    text
      .split("\n")
      .map((l) => l.trim())
      .find((l) => l.length > 0) ?? "";
  const MAX = 48;
  if (firstLine.length <= MAX) return firstLine || "Réponse de Bob";
  return `${firstLine.slice(0, MAX - 1).trimEnd()}…`;
}

/** Ingest a sub-task's structured deliverable, if it has one. The task carries
 * its own title + goal, so the projection input is a direct subset. */
function ingestTask(
  task: Task,
  add: (input: {
    id: string;
    deliverable: ComponentDescriptor[];
    task: DeliverableCardTask;
  }) => void,
): void {
  const payload = task.resultPayload;
  if (!payload || payload.length === 0) return;
  add({
    id: task.id,
    deliverable: payload,
    task: { title: task.title, goal: task.goal },
  });
}

/** Ingest a persisted Bob message's `ui` (already a `ComponentDescriptor[]`).
 * Keyed by the message id (= the streamed `msg_id`) so it dedupes against the
 * in-flight copy. */
function ingestMessage(
  message: ChatMessage,
  add: (input: {
    id: string;
    deliverable: ComponentDescriptor[];
    task: DeliverableCardTask;
  }) => void,
): void {
  if (message.role !== "assistant") return;
  const ui = message.ui;
  if (!ui || ui.length === 0) return;
  add({
    id: message.id,
    deliverable: ui,
    task: { title: deriveTitle(message.content) },
  });
}

/**
 * Subscribe to the live deliverable sources and feed the store. Mount this once
 * inside the dock (or DataSlot). Returns nothing — it's a side-effect bridge.
 *
 * The effect re-runs whenever `tasks`, `messages`, or the in-flight
 * `streamingAssistant` change. On each pass it (re-)ingests every current
 * deliverable; `deliverableStore.add` dedupes by id so this never produces
 * duplicate cards, and a replayed/streamed copy of the same artefact collapses
 * to a single entry.
 */
export function useDeliverableIngest(): void {
  const tasks = useChatStore((s) => s.tasks);
  const messages = useChatStore((s) => s.messages);
  const streamingAssistant = useChatStore((s) => s.streamingAssistant);
  const add = useDeliverableStore((s) => s.add);

  useEffect(() => {
    // 1. Sub-task deliverables (task_result.result_payload).
    for (const task of Object.values(tasks)) {
      ingestTask(task, add);
    }

    // 2. Bob's ui_payload — persisted messages (ui is already a list).
    for (const message of messages) {
      ingestMessage(message, add);
    }

    // 2b. Bob's IN-FLIGHT ui_payload — a single descriptor on the streaming
    // buffer, wrapped to a list-of-one. Keyed by the live `msgId` so when the
    // turn closes the persisted `messages[].ui` (same id) dedupes to this one
    // card rather than spawning a second.
    if (streamingAssistant?.ui) {
      add({
        id: streamingAssistant.msgId,
        deliverable: [streamingAssistant.ui],
        task: { title: deriveTitle(streamingAssistant.speech) },
      });
    }
  }, [tasks, messages, streamingAssistant, add]);
}
