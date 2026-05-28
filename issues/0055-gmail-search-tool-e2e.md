## Parent

`prd/0007-gmail-mail-overlay.md`

## What to build

Wire the Gmail connector to the sub-agent runtime as a new `gmail_search` tool, plumb the result through `say.ui` Mail, and update Jarvis + sub-agent system prompts so the LLM picks the right path. End-to-end: user says "trouve-moi le dernier mail d'Holyana Callejon" → Jarvis spawns a background sub-task → sub-agent calls `gmail_search` → Mail overlay opens with the real message.

Tool registration: add `build_gmail_search_tool()` to `bob.sub_agent.tool_registry` returning a `SubAgentToolDefinition(name="gmail_search", version="v1", args_model=GmailSearchArgs, handler=_gmail_search_handler, description=…)`. Register it inside `build_default_subagent_registry()`.

`GmailSearchArgs` (Pydantic): `from_name: str | None`, `from_email: str | None`, `subject_contains: str | None`, `after: str | None`, `before: str | None`, `has_attachment: bool | None`, `label: str | None`, `max_results: int = 1` (capped at 5). Validator rejects all-None payload.

Handler `_gmail_search_handler`: validate args, build query via `query_builder.build_query(...)`, instantiate `GmailClient(auth.get_credentials())`, call `search_messages(query, max_results)`, return a structured outcome with the list of `to_mail_props(msg)` dicts (sub-agent then chooses one and emits `say.ui`).

Sub-agent system prompt: append a paragraph stating that when the goal is an email lookup, the sub-agent should call `gmail_search` with the most specific args it can infer from the goal, then conclude with `say(speech=meta_summary, ui={component:"Mail", props:result})`. Meta summary should be short: "Mail de {from_name}, sujet '{subject}', reçu {relative_time}".

Jarvis system prompt (`backend/prompts/system_chat.md`): append a single line in the capabilities section mentioning that Bob can find emails for the user via a research sub-task. No tool name leaked at the Jarvis level — Jarvis only needs to know the capability exists to route via `spawn_task`.

Live status feedback: the sub-agent emits a reflection event ("recherche Gmail") before the tool call and another ("lecture du mail") before the final `say`, so the HudTasks row shows progress text during the task lifetime.

## Acceptance criteria

- [ ] `build_gmail_search_tool()` defined; registered in `build_default_subagent_registry()`.
- [ ] `GmailSearchArgs` Pydantic model with all eight fields + cap on `max_results`; rejects all-None args.
- [ ] Handler builds query via `query_builder`, calls `GmailClient.search_messages` with refreshed credentials, returns `to_mail_props`-shaped result list.
- [ ] Sub-agent system prompt updated with email-lookup paragraph (gmail_search + Mail component contract + meta summary format).
- [ ] `backend/prompts/system_chat.md` mentions email-finding capability in Jarvis capabilities section.
- [ ] Sub-agent emits reflection events ("recherche Gmail", "lecture du mail") visible in HudTasks row during run.
- [ ] On success path: user message "trouve-moi le dernier mail d'Holyana Callejon" → spawn_task → sub-agent runs → Mail overlay opens with real message data, Bob speaks meta summary.
- [ ] Tool handler unit test: mocked `GmailClient` returns canonical `EmailMessage` fixture → handler returns expected dict shape matching `Mail` schema.
- [ ] Tool handler unit test: `GmailClient` raises → handler returns structured failure outcome (not raised through dispatcher).
- [ ] Sub-agent runner integration test: stub LLM emits a `gmail_search` tool call followed by `say` with Mail ui; runner produces expected final `say.ui` payload + reflection events.
- [ ] End-to-end manual demo: voice or text query for a known sender opens the Mail overlay with correct data within ~2 s (HITL).

## Blocked by

`issues/0053-mail-ui-component-overlay.md`
`issues/0054-gmail-connector-package.md`
