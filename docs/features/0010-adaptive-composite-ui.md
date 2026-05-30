# Adaptive Composite UI (sections list overlay)

Shipped on 2026-05-30 from PRD `prd/0010-adaptive-composite-ui.md`.

## What it does

Bob now renders visual answers as **stacked sections** instead of a single card. A response is an ordered list of sections (a mail, another mail, a Markdown block…) shown in one scrollable HUD overlay with a single frame. "Donne-moi mes 3 derniers mails" produces 3 stacked Mail cards, each with its own inline actions (OPEN, READ ALOUD) — fixing the long-standing bug where only the first mail surfaced. A single-element answer stays a one-section view (no visual regression). Unknown or malformed sections degrade gracefully: an unsupported component shows a readable "Section non supportée" card, and a single bad section never blanks its valid siblings.

## Technical surface

- **Deliverable contract** — the sub-agent terminal deliverable and Jarvis `say.ui` both speak `ComponentDescriptor[]`. `ProjectedResult.deliverable` is `list | None` (empty normalized to `None`); `default_projector` still yields `None`.
- **Persistence** — `Task.result_payload` is stored/read as a JSON array. Decoding is **defensive**: any non-list value (legacy single object, `null`, corrupt JSON, scalar) is read back as an empty list and never raises. No back-fill — old rows are rendered harmless, not migrated.
- **Gmail projector** — `project_gmail_search` emits one `Mail` section per returned message in result order (was `messages[0]`). The transcript digest and deterministic spoken summary ("N email(s) trouvé(s)…") are unchanged.
- **Section-list validator** — `ui_registry.validate_sections(sections) -> (kept, errors)` validates section by section against the existing per-component `oneOf` schema (no new schema). Invalid/unknown sections are dropped and reported (`sections[i]:` prefix, same string shape as `validate_component_descriptor`); valid siblings are kept in order. `runner._validate_sections` delegates to it.
- **Frontend** — new `SectionsOverlay` is the single overlay shell (corner-bracket frame, header, global DISMISS, Esc, backdrop, scrollable stack). A section registry maps component name → `{ Component, structured }`. MVP catalogue: `Mail` (structured), `Markdown` (text), `NotImplemented` (fallback). `SphereUI` holds a single `overlaySections` state with one entry point `openOverlayFromSections`. `MailCard` and `MarkdownSection` are chrome-free section bodies extracted from the old overlays.

## Notable decisions

- **Bare list, no wrapper.** The canonical payload is `ComponentDescriptor[]` — no `View`/`Sections` wrapper, no `sections` key. `say.ui` and `result_payload` converged on the same shape so the two surfaces cannot diverge.
- **Hybrid composition.** Deterministic code produces data-section props; the LLM only picks *which* result to surface via a single `result_ref` in `done`, which expands to the full projected section list. The weak local model (`qwen3.5-9b`) never enumerates messages nor writes card props — robustness over model quality.
- **Auto-open by structure.** A list containing ≥1 `structured` section opens the overlay unconditionally; a text-only (Markdown) list defers to the existing `shouldOverlayResponse` heuristic. Minor shift: short unstructured Markdown deliverables now stay inline rather than popping the overlay. Source dedup (msg id, task id) does not reopen after dismiss.
- **Big-bang removal.** The mono-component `MailOverlay` and `MarkdownOverlay` are deleted; `TaskOverlay` stays and consumes the same section list.
- **Robustness invariants** — invalid section never blanks the view (per-section drop), unknown section never crashes (NotImplemented fallback), non-list `result_payload` never raises (defensive decode), data props stay deterministic.

## Issues

- `issues/0066-sections-list-pipeline-markdown.md` — Sections-list deliverable pipeline + SectionsOverlay (Markdown) — commit ecc5c1d
- `issues/0067-multi-mail-sections.md` — Multi-mail sections (one Mail card per message) — commit 5a8cc13
- `issues/0068-per-section-drop-validation.md` — Per-section drop validation for section lists — commit 00a6d05
