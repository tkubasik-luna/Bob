# P7a — Deliverable transport + dispatch (fix Mail overlay)

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Fix the user-visible Mail-overlay bug **without touching the codec**. The sub-agent already emits a correct `{"component":"Mail","props":{…}}` deliverable (verified in `backend/logs/orchestration.jsonl`, the sonnet runs of 2026-05-28), but the descriptor is dropped at four points on the way to the screen. Carry it structured end-to-end:

1. `_deliverable_text` (`runner.py:196-213`) stops flattening a `{component, props}` descriptor to text.
2. Persistence widens to hold a structured deliverable — `task.result` is `str | None` today (`task_store.py:75`, `:339`, `:494`); store the descriptor (e.g. a `result_payload` JSON column) alongside the spoken `result_summary`.
3. The completion event carries the descriptor (`runner.py:1176-1182`).
4. The frontend task-result effect (`SphereUI.tsx:239-259`) dispatches on `component` via the existing `openOverlayFromDescriptor` (`:156-179`, already routes Markdown vs Mail) instead of always calling `setOverlayContent`.
5. `show_task_result` (`show_task_result.py:166-169`) re-emits the stored descriptor's original `component`, not a forced `Markdown` wrap.

Carry the 0056 privacy redaction (`_redact_ui_payload_for_debug`, `runner.py:236-268`) onto the new structured persistence and completion event so subject / body never leak into the debug ring buffer, the `/ws/debug` feed, or the JSONL sink. This slice is independent of the codec work and can ship immediately.

## Acceptance criteria

- [ ] A completed Gmail-search sub-task renders the Mail overlay (not a Markdown blob, not nothing)
- [ ] Descriptor persisted structured and shipped on the completion event; `_deliverable_text` no longer flattens a descriptor
- [ ] Frontend task-result effect routes Markdown → MarkdownOverlay, Mail → MailOverlay
- [ ] `show_task_result` re-emits the stored descriptor with its original `component`
- [ ] Privacy: subject / `bodyPreview` / snippet never appear in a `DebugEvent` payload or the JSONL sink after the change (test asserts)
- [ ] Markdown deliverables (non-Mail tasks) still render unchanged

## Blocked by

None - can start immediately
