## Parent

`prd/0010-adaptive-composite-ui.md`

## What to build

Convert the whole LLM→UI deliverable contract onto a **list of sections** (`ComponentDescriptor[]`), proving the full pipeline end-to-end on the simplest existing case: a Markdown deliverable. A single Markdown result now travels as a list-of-one and renders through a new unified `SectionsOverlay`, replacing the standalone `MarkdownOverlay`.

End-to-end path:

- **Backend** — the sub-agent terminal deliverable resolves to `list[ComponentDescriptor] | None` (empty list normalized to `None`); `ProjectedResult.deliverable` becomes a list; the persisted `result_payload` is stored/read as a JSON array; the WS `task_result` carries the list. The `result_payload` decode is **defensive**: any non-list value (legacy single object, `null`, corrupt JSON) is read back as an empty list — never raises.
- **Frontend** — a new `SectionsOverlay` is the single overlay shell (corner-bracket frame, header, global DISMISS, Esc, backdrop, scrollable stack). A section registry maps component name → renderer; the MVP entry is `Markdown` (text, non-structured). An unknown component renders a minimal `NotImplemented` card ("Section non supportée : <ComponentName>" + hint), never the raw props, never a crash. `SphereUI` holds a single `overlaySections: ComponentDescriptor[] | null` state with one dispatch entry point; auto-open for a text-only list keeps the existing `shouldOverlayResponse` heuristic. The old `MarkdownOverlay` is removed.

Verifiable on its own: a Markdown task result still surfaces (now via `SectionsOverlay`) with no visual regression, and an unknown section degrades to a NotImplemented card.

## Acceptance criteria

- [ ] The terminal deliverable resolver returns `list[ComponentDescriptor] | None`; an empty result yields `None`.
- [ ] `ProjectedResult.deliverable` is a list; `default_projector` still yields `None`.
- [ ] `result_payload` is persisted and read as a JSON array; the WS `task_result` carries the list.
- [ ] Decoding a non-list `result_payload` (legacy single object, `null`, corrupt JSON) returns an empty list and never raises.
- [ ] `SectionsOverlay` renders a list of sections in order inside one shell, with scroll past the viewport height and DISMISS / Esc / backdrop close paths.
- [ ] A `Markdown` section renders its content via the section registry.
- [ ] An unknown component name renders a `NotImplemented` card showing the component name, with no raw props and no crash.
- [ ] `SphereUI` uses a single `overlaySections` state; a text-only list opens the overlay per the existing `shouldOverlayResponse` heuristic; source dedup does not reopen after dismiss.
- [ ] `MarkdownOverlay` (standalone mono-component overlay) is removed.
- [ ] Tests cover: deliverable resolver, defensive codec round-trip, section registry / NotImplemented, SectionsOverlay render + close, auto-open dispatch.

## Blocked by

None - can start immediately.
