import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, test } from "vitest";
import { useChatStore } from "../../store/chatStore";
import type { Task } from "../../types/ws";
import { HudTasks } from "./HudTasks";

// Snapshot the pristine store so each test starts from a clean slate. Zustand
// exposes `setState` / `getState` on every hook by default.
const initialState = useChatStore.getState();

function makeTask(overrides: Partial<Task> & Pick<Task, "id" | "state">): Task {
  return {
    id: overrides.id,
    title: overrides.title ?? `Tâche ${overrides.id}`,
    goal: overrides.goal ?? "",
    state: overrides.state,
    createdAt: overrides.createdAt ?? "2024-01-01T00:00:00.000Z",
    needsAttention: overrides.needsAttention,
    progressStatus: overrides.progressStatus,
    result: overrides.result,
    updatedAt: overrides.updatedAt,
  };
}

function setTasks(tasks: Task[]): void {
  const map: Record<string, Task> = {};
  for (const t of tasks) map[t.id] = t;
  useChatStore.setState({ tasks: map });
}

describe("HudTasks", () => {
  beforeEach(() => {
    // Restore every field, not just `tasks`, so neighbouring tests can't
    // bleed state in (the store is a single shared singleton).
    useChatStore.setState(initialState, true);
  });

  test("renders header `00/00` and no rows when the store is empty", () => {
    const { container } = render(<HudTasks />);
    const count = container.querySelector(".hud-tasks-count");
    expect(count).not.toBeNull();
    expect(count?.textContent?.replace(/\s+/g, "")).toBe("00/00");
    expect(container.querySelectorAll(".hud-task")).toHaveLength(0);
  });

  test("running task renders `.is-running` with spinner DOM + progressStatus override", () => {
    setTasks([
      makeTask({
        id: "t1",
        state: "running",
        title: "Synthèse · réunion",
        progressStatus: "j'analyse 3/10",
      }),
    ]);
    const { container } = render(<HudTasks />);
    const card = container.querySelector(".hud-task");
    expect(card).not.toBeNull();
    expect(card?.classList.contains("is-running")).toBe(true);
    // The spinner is the `::before` pseudo-element on `.hud-task-status`,
    // but the host element itself is the spinner anchor — assert the slot
    // exists so the CSS spinner is mountable.
    expect(card?.querySelector(".hud-task-status")).not.toBeNull();
    expect(card?.querySelector(".hud-task-sub")?.textContent).toBe("j'analyse 3/10");
  });

  test("done task renders `.is-done` with sub `OK` (check icon via CSS)", () => {
    setTasks([makeTask({ id: "t1", state: "done", title: "Veille · IA" })]);
    const { container } = render(<HudTasks />);
    const card = container.querySelector(".hud-task");
    expect(card).not.toBeNull();
    expect(card?.classList.contains("is-done")).toBe(true);
    expect(card?.querySelector(".hud-task-sub")?.textContent).toBe("OK");
    // The check icon lives on `.hud-task-status::before` (CSS); assert the
    // anchor element exists so the icon can be rendered.
    expect(card?.querySelector(".hud-task-status")).not.toBeNull();
  });

  test("failed task renders `.is-error` with sub `ÉCHEC` (cross icon via CSS)", () => {
    setTasks([makeTask({ id: "t1", state: "failed", title: "Téléchargement · 3 Go" })]);
    const { container } = render(<HudTasks />);
    const card = container.querySelector(".hud-task");
    expect(card).not.toBeNull();
    expect(card?.classList.contains("is-error")).toBe(true);
    expect(card?.querySelector(".hud-task-sub")?.textContent).toBe("ÉCHEC");
  });

  test("renders only the last 4 tasks when 5 are in the store", () => {
    setTasks([
      makeTask({ id: "a", state: "done", createdAt: "2024-01-01T00:00:01.000Z" }),
      makeTask({ id: "b", state: "running", createdAt: "2024-01-01T00:00:02.000Z" }),
      makeTask({ id: "c", state: "running", createdAt: "2024-01-01T00:00:03.000Z" }),
      makeTask({ id: "d", state: "running", createdAt: "2024-01-01T00:00:04.000Z" }),
      makeTask({ id: "e", state: "running", createdAt: "2024-01-01T00:00:05.000Z" }),
    ]);
    const { container } = render(<HudTasks />);
    const rows = container.querySelectorAll(".hud-task");
    expect(rows).toHaveLength(4);
    // The oldest task ("a") falls off the end of `slice(-4)`.
    const ids = Array.from(rows).map((r) => r.getAttribute("data-task-id"));
    expect(ids).toEqual(["b", "c", "d", "e"]);
  });

  test("count badge shows live/total and applies `.is-live` when running > 0", () => {
    setTasks([
      makeTask({ id: "r1", state: "running" }),
      makeTask({ id: "r2", state: "running" }),
      makeTask({ id: "q1", state: "pending" }),
      makeTask({ id: "d1", state: "done" }),
    ]);
    const { container } = render(<HudTasks />);
    const liveSpan = container.querySelector(".hud-tasks-count > span:first-child");
    expect(liveSpan?.textContent).toBe("03"); // 2 running + 1 pending = 3 live
    expect(liveSpan?.classList.contains("is-live")).toBe(true);
    const totalSpan = container.querySelector(".hud-tasks-count > span:last-child");
    expect(totalSpan?.textContent).toBe("04");
  });

  test("count badge omits `.is-live` when no task is running", () => {
    setTasks([makeTask({ id: "q1", state: "pending" }), makeTask({ id: "d1", state: "done" })]);
    const { container } = render(<HudTasks />);
    const liveSpan = container.querySelector(".hud-tasks-count > span:first-child");
    expect(liveSpan?.classList.contains("is-live")).toBe(false);
  });

  test("pending task uses `.is-queued` with sub `EN FILE`", () => {
    setTasks([makeTask({ id: "p1", state: "pending" })]);
    const { container } = render(<HudTasks />);
    const card = container.querySelector(".hud-task");
    expect(card?.classList.contains("is-queued")).toBe(true);
    expect(card?.querySelector(".hud-task-sub")?.textContent).toBe("EN FILE");
  });

  test("waiting_input task uses `.is-queued` with sub `ATTENTE INPUT`", () => {
    setTasks([makeTask({ id: "w1", state: "waiting_input", needsAttention: true })]);
    const { container } = render(<HudTasks />);
    const card = container.querySelector(".hud-task");
    expect(card?.classList.contains("is-queued")).toBe(true);
    expect(card?.classList.contains("needs-attention")).toBe(true);
    expect(card?.querySelector(".hud-task-sub")?.textContent).toBe("ATTENTE INPUT");
  });

  test("running task without `progressStatus` falls back to neutral `EN COURS`", () => {
    setTasks([makeTask({ id: "r1", state: "running" })]);
    const { container } = render(<HudTasks />);
    const card = container.querySelector(".hud-task");
    expect(card?.querySelector(".hud-task-sub")?.textContent).toBe("EN COURS");
  });

  test("re-renders rows live when the store changes", () => {
    const { container } = render(<HudTasks />);
    expect(container.querySelectorAll(".hud-task")).toHaveLength(0);

    act(() => {
      setTasks([makeTask({ id: "t1", state: "running" })]);
    });
    expect(container.querySelectorAll(".hud-task")).toHaveLength(1);
    expect(container.querySelector(".hud-task")?.classList.contains("is-running")).toBe(true);
  });
});
