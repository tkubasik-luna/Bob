# Web Search Tool (Tavily)

Shipped on 2026-06-04. Built directly (no PRD/issues flow); reuses the
Gmail connector (0008) architecture end to end.

## What it does

The user can ask Bob anything that needs the live internet — "cherche les
dernières news sur X", "quelle est la capitale de Y", "trouve-moi un article
sur Z" — and Bob delegates the research to a background sub-task. The
sub-agent calls `web_search` (ranked results + Tavily's optional direct
answer), and when a result is worth reading in full, `web_fetch` extracts that
page's text; it then synthesises a short French answer. The sources surface as
a dedicated `WebResults` overlay card in the HUD data dock (a globe glyph in
the dock; clickable result titles open the source in the default browser). For
a snippet-answerable question Bob speaks the answer and the card carries the
sources; for a deeper question Bob writes a Markdown synthesis citing its
sources. On any error (no key, bad key, quota, network) Bob explains in French
what to do — never a broken state.

## Technical surface

- **New backend package — `bob.connectors.tavily`** — `client`
  (`TavilyClient`, a thin async wrapper over the Tavily Search / Extract REST
  API via `httpx.AsyncClient`, mockable at the transport layer through a
  `client_factory` seam), `models` (`WebSearchResults` / `WebSearchResult` /
  `WebPage` dataclasses + pure `from_tavily_*` factories + the
  `to_web_results_props` adapter to the `WebResults` UI props), `errors`
  (failure taxonomy). Authentication is a `Bearer` header. Independent of
  `bob.tools` / `bob.ui_registry`, exactly like the gmail connector.
- **Error taxonomy** — `TavilyError` base with `MissingApiKeyError`,
  `UnauthorizedError` (401/403), `RateLimitedError` (429), `ApiUnreachableError`
  (network / timeout / 5xx). The HTTP boundary owns the classification so the
  handler stays a thin translator.
- **Sub-agent tools** — `web_search` (v1, `WebSearchArgs`: `query` +
  optional `max_results` 1–10) and `web_fetch` (v1, `WebFetchArgs`: `url`,
  validated http(s)) registered in `build_default_subagent_registry()`.
  Handlers map every connector exception to structured codes
  (`web_search_missing_key` / `_unauthorized` / `_rate_limited` /
  `_api_unreachable` / `_failed`, and the `web_fetch_*` mirror). Both tools are
  **non-terminal** — a research sub-agent searches, optionally fetches, then
  synthesises, so the runner never converges on them.
- **Deterministic projections** (PRD 0009/0010) — `project_web_search` builds
  the `WebResults` card + a spoken summary (Tavily's `answer`, else a count
  line) from the stored result; its digest keeps the snippets (the model's
  working material) but caps count + length. `project_web_fetch` keeps a
  capped page excerpt in the digest and emits a Markdown "page I read" card.
  Both projections always emit a card so a stall right after a tool call
  surfaces something (the runner finalises a forced exit from `last()`), never
  an empty overlay.
- **Prompts** — a goal-triggered `WEB_SEARCH_SKILL_PACK` (in
  `bob.context.prompt_fragments`) carries the recipe: build a focused query →
  converge on the search via `result_ref` (the card is rebuilt
  deterministically) for a snippet-answerable goal, OR `web_fetch` + Markdown
  synthesis for a read-in-full goal → and the five tool-ERROR branches with
  pinned, verbatim French speech (read aloud via TTS). Triggers: `web`,
  `internet`, `en ligne`, `google`, `actualité(s)`, `news`, `météo`,
  `wikipedia`, `sur le net`. Jarvis's system prompt gains a single capability
  line ("chercher des informations sur internet … via `spawn_task`") — no tool
  name leaked, mirroring the gmail routing pattern.
- **UI component** — `WebResults` registered in
  `bob.ui_registry.build_registry()` with a strict JSON schema
  (`query`, optional `answer`, `results[]` of `{title, url (http(s) pattern),
  snippet?}`). Validation rejects malformed props before the wire.
- **Frontend** — new `WebResultsCard.tsx` (chrome-free overlay surface: the
  answer lead + a list of clickable source rows) wired into the shared
  `sectionRegistry`; `overlayArtifact` gains a `WEB` chip + a speech branch
  (answer + titles, no URLs); `SectionsOverlay.firstOpenableUrl` opens the
  first result; `deliverableCard` gains a `web` dock type (globe glyph + `WEB`
  label) + content-summary fallback. `ComponentDescriptor` gains a typed
  `WebResults` branch with a `WebResultsProps` mirror; an unknown component
  still degrades to the generic doc card.
- **Config** — `TAVILY_API_KEY` (free-tier key from https://app.tavily.com),
  `TAVILY_BASE_URL`, `TAVILY_TIMEOUT_SECONDS`, `WEB_SEARCH_MAX_RESULTS` added
  to `bob.config`. The key is **optional**: when unset the handlers return an
  actionable `web_search_missing_key` error rather than failing the boot (it
  is only needed at call time).
- **Deps** — `httpx` promoted from a dev-only to a runtime dependency in
  `backend/pyproject.toml`.

## Notable decisions

- **Tavily over Brave / DuckDuckGo / a raw SERP proxy.** Tavily is built for
  LLM agents: clean snippets + an optional synthesised `answer`, one API key,
  and an `extract` endpoint that maps 1:1 onto `web_fetch`. Robustness was the
  deciding factor (the no-key DuckDuckGo route is the most fragile).
- **Raw `httpx`, not the `tavily-python` SDK.** The Tavily REST surface is one
  POST per operation; raw `httpx` gives full control over timeout + error
  mapping with one fewer dependency, and `httpx` was already in the tree.
- **Lean connector package, not inline.** Unlike gmail (OAuth) the integration
  is simple, but a `client`/`models`/`errors` package keeps the handler thin
  and mockable at the boundary — the same test ergonomics as gmail's
  `service_factory`.
- **Both tools non-terminal.** Web research is a chain (search → fetch →
  synthesise), unlike the single-shot mail lookup, so neither tool sets
  `terminal=True`; the model drives convergence via `done(result_ref=…)`
  (quick lookup) or `done(ui_payload=Markdown)` (synthesis).
- **`web_fetch` carries a card too.** Even though a fetch is intermediate, its
  projection emits a Markdown excerpt card so a stall right after a fetch still
  shows the page Bob read (the anti-stall invariant from PRD 0010).
- **Reused the existing overlay/dock pipeline.** No new render path — the
  `WebResults` card slots into the same `sectionRegistry` /
  `SectionsOverlay` / data-dock machinery as Mail and Markdown.

## Files

- Backend: `bob/connectors/tavily/{__init__,client,models,errors}.py`,
  `bob/sub_agent/tool_registry.py`, `bob/context/prompt_fragments.py`,
  `bob/ui_registry.py`, `bob/config.py`, `prompts/system_chat.md`,
  `pyproject.toml`.
- Frontend: `components/sphere/WebResultsCard.tsx`,
  `components/sphere/{sectionRegistry,overlayArtifact,SectionsOverlay}.{ts,tsx}`,
  `components/sphere/SectionsOverlay.css`, `components/piste/DataCard.tsx`,
  `lib/deliverableCard.ts`, `types/ws.ts`.
- Tests: `tests/connectors/tavily/test_tavily_{client,models}.py`,
  `tests/test_web_search_{tool,projector}.py`, and frontend
  `WebResultsCard.test.tsx` + extensions to the sectionRegistry / overlayArtifact
  / deliverableCard suites.

## Setup

Add a free-tier key from https://app.tavily.com to `.env`:

```
TAVILY_API_KEY=tvly-...
```

No key → web search returns an actionable error ("ajoute une clé Tavily …");
everything else still builds and the suite stays green.

## HITL smoke tests (deferred to user)

- Ask Bob a factual question ("quelle est la capitale de l'Australie ?") — Bob
  speaks the answer within a few seconds and a `WebResults` card appears in the
  dock with clickable sources.
- Ask a deeper question that warrants reading a page ("résume l'article
  Wikipédia sur la tour Eiffel") — Bob `web_fetch`es the top result and speaks
  a synthesised French summary; the dock card carries the source.
- Blank `TAVILY_API_KEY` and retry — Bob says the "ajoute une clé Tavily
  (TAVILY_API_KEY) dans le fichier .env" sentence; the task is marked failed,
  no broken overlay.
- Click a result title in the `WebResults` card — the source opens in the
  default browser.
