# Investigation — Mail overlay stays empty when the sub-agent stalls (data is retrieved, display is not)

**Date:** 2026-05-30
**Reporter:** "regarde les derniers logs, il m'a bien sorti les bonnes données, mais l'affichage ne va pas du tout"
**Provider/model:** `lm_studio` · `qwen/qwen3.5-9b` (small 9B local model), guided JSON ON
**Logs:** `backend/logs/orchestration.jsonl`, `backend/logs/llm-2026-05-30.jsonl`
**Task:** `959d3366852e4cc28027af99b75f85ec` — *"Rechercher et afficher le dernier email reçu dans la boîte de réception"* (2026-05-30 11:52:09 → 11:52:29)

## TL;DR

The Gmail tool worked **on the first call**: `gmail_search {label:"INBOX", max_results:1}`
returned exactly one message (a TestFlight notification, *"KiLi DEV 2.9.0 (205) for iOS
is now available to test"*). The sub-agent then **failed to terminate**: it emitted four
consecutive `progress` thoughts all saying *"j'attends maintenant la réponse de l'outil"*
— even though the result was already sitting in its transcript. The stall guard
(2026-05-29 fix) caught it and force-terminated at iteration 5 with
`done(status=degraded, reason_code=stalled_no_progress)`.

That forced `done` carries **`ui_payload=null`**. The salvage path restores only the
*text* (`result_summary`) — by design it does **not** rebuild the structured Mail
descriptor. So:

- **Speech is correct** — the salvaged text feeds the done-synthesis and Jarvis says
  *"Voilà ce que j'ai trouvé… une notification TestFlight, KiLi 2.9.0…"* → "les bonnes données".
- **The display is empty** — `done.ui_payload=null` → `task.result_payload=null` → the
  `task_result` frame omits `result_payload` → the **Mail overlay has nothing to render**
  → "l'affichage ne va pas du tout".

Root cause is the **sub-agent control loop**, same class as the 2026-05-29 mail-tool-loop
(RC1, convergence). What is new here is the *symptom*: the prior fix made the **text**
survive a forced stall; the **structured Mail deliverable still dies**, because the
salvage path was deliberately built text-only ("Mail reconstruction lives in the skill
pack" — i.e. in the model's `done`, which is exactly what stalls).

## Timeline

| time (11:52) | event |
|------|-------|
| :09.887 | task spawned (`spawn_task → ok`); Jarvis: *"D'accord, je m'en occupe."* |
| :09 → :13 | LLM call 1 (3925 ms, 35 tok) → `tool_call gmail_search {label:"INBOX", max_results:1}` |
| :13.822 → :14.724 | **`gmail_search` → `ok`, `count:1`** — the TestFlight/KiLi mail, full `bodyPreview` |
| :16.461 | LLM call 2 → `progress` *"…J'attends maintenant la réponse de l'outil."* (stall 1) |
| :19.208 | LLM call 3 → `progress` (same content, stall 2 → `system_validator` nudge injected) |
| :21.970 | LLM call 4 → `progress` (stall 3) — nudge ignored |
| :24.018 | LLM call 5 → `progress` (stall 4) — force threshold reached |
| :24.022 | runner forces `done(status=degraded, reason_code=stalled_no_progress)` — 5 iters, 10 787 tok, 14.1 s |
| :24.025 | `task_result` WS event = `{result: null}` (**no `result_payload`**) |
| :27.263 | Jarvis (proactive): *"Voilà ce que j'ai trouvé… notification TestFlight, KiLi 2.9.0… Voulez-vous le lien complet ?"* — `ui: []` |

Only **one** `gmail_search` this run (no duplicate-call storm like 2026-05-29). The
failure is **pure `progress`-spam after a successful result** — the path the
2026-05-29 "Trou A" fix bounds (force at 4 consecutive `progress`).

## Evidence

**The first (and only) tool result already held the answer** (`tool`-role message in the
transcript, `llm-2026-05-30.jsonl`):
```json
{"tool":"gmail_search","status":"ok","result":{"query":"label:INBOX","count":1,
 "messages":[{"from":{"name":"l'école des loisirs via TestFlight",
   "email":"testflight_no_reply@email.apple.com"},
   "receivedAt":"2026-05-29T15:04:47Z",
   "subject":"KiLi DEV 2.9.0 (205) for iOS is now available to test.",
   "bodyPreview":"KiLi DEV 2.9.0 (205) is ready to test on iOS. …",
   "gmailWebUrl":"https://mail.google.com/mail/u/0/#inbox/19e744436f754b1f"}]}}
```

**The recipe told the model exactly what to emit** (sub-agent system prompt, "Cas
spécial — recherche d'un mail"):
> 3. Une fois un résultat NON VIDE reçu, émets `progress(thought="lecture du mail")` puis
> termine avec un `done` dont `ui_payload` est un OBJET … de la forme
> `{"component": "Mail", "props": <props>}` où `<props>` est le premier élément de la
> liste `messages` retournée par `gmail_search`.

The model never executed step 3. Across calls 2–5 it re-emitted the *call 1* narration
(*"J'ai appelé gmail_search … J'attends maintenant la réponse de l'outil"*), apparently
not recognising that the `tool` result was already in context. The `system_validator`
nudge injected at stall 2 ("tu as déjà un résultat — émets `done`") was also ignored — a
9B-model limitation, not a prompt gap.

**The forced `done` payload** (`orchestration.jsonl`, redacted DEBUG mirror):
```json
{"task_id":"959d…","status":"degraded","reason_code":"stalled_no_progress",
 "result":null,"ui_payload":null,
 "cost":{"iterations":5,"tokens_estimate":10787,"elapsed_seconds":14.13}}
```
(`result`/`ui_payload` read `null` here because the JSONL sink is the privacy-redacted
mirror, issue 0056; the chat client received the full salvaged `result` text — which is
why Jarvis could speak the mail.)

**The proactive announcement** (`assistant_msg`):
```json
{"type":"assistant_msg","speech":"Voilà ce que j'ai trouvé … notification TestFlight,
 KiLi 2.9.0 … Voulez-vous le lien complet ?","ui":[],"proactive":true}
```
`ui: []` — no Mail component. The deliverable does **not** ride `assistant_msg`; it rides
the `task_result` channel, which carried `null`.

## Root causes (priority order)

### RC1 — Model has the result but never emits `done` *(primary)*
`SubAgentRunner._run` loops on `progress`/`tool_call` and exits only when the model emits
`done` (or a cap/stall guard fires). The 9B model received a clean, non-empty
`gmail_search` result and then spun on filler `progress` instead of emitting the
prescribed `done({component:"Mail", …})`. Neither the recipe instruction nor the
stall-2 `system_validator` nudge moved it. The model effectively does not recognise that
the tool result is already available — convergence cannot be guaranteed by prompting a
weak local model.

### RC2 — The salvage path rebuilds text but not the Mail deliverable *(makes it a display bug)*
On a forced stall, `_force_stalled_done` salvages the last successful tool result via
`_salvage_tool_result_text` (`backend/src/bob/sub_agent/runner.py:278`), which folds a
**string** into `result_summary` and explicitly states *"no Mail-overlay reconstruction
here — that lives in the skill pack"* (`runner.py:287`). So `ui_payload` stays `null`.
The Mail descriptor is **deterministically reconstructable** from the very result we
already salvage as text — the recipe even specifies the exact shape
(`{component:"Mail", props: messages[0]}`) — but nothing builds it on the forced-exit
path. This is the gap deferred on 2026-05-29; today it is the user-visible symptom.

### RC3 — `ui_payload=null` propagates cleanly to an empty overlay *(transport, working as designed)*
`done.ui_payload` is persisted as `task.result_payload` (issue 0064,
`task_store.py:347-368`). The live `task_result` frame includes it **only when non-null**
(`ws_router.py:314`: `if task.result_payload is not None`). With `result_payload=null`,
the frame is `{type:"task_result", result:null}` and the Mail overlay (which keys on
`result_payload` → `{component, props}`) renders nothing. The transport is correct; it was
handed a `null`. Note `_push_proactive_assistant_msg` hardcodes `ui:[]`
(`orchestrator.py:1261/1271`) — by design, the structured deliverable never travels on
`assistant_msg`, so fixing the overlay means fixing `result_payload`, not the proactive `ui`.

## Recommended fixes

1. **Deterministic deliverable salvage (RC2) — primary.** On any forced terminal `done`
   (stall / iteration_cap / token_cap), if the last successful tool result has a
   registered *deliverable builder*, reconstruct `ui_payload` from it instead of leaving
   it `null`. Keep the runner tool-agnostic: a small registry maps `tool_name →
   builder(result) -> ComponentDescriptor | None`; Gmail registers
   `gmail_search → {component:"Mail", props: messages[0]}` when `count > 0`. This is
   exactly the `done` the model was supposed to emit, computed deterministically. Mirrors
   the existing text salvage (`_salvage_tool_result_text`) but for the structured payload.
   After this, a stalled mail task renders the Mail overlay *and* speaks the summary.

2. **Converge on the first usable result (RC1) — defence in depth.** For a mail-class
   task the recipe already says *"une fois un résultat NON VIDE reçu … termine"*. Enforce
   it: after the first successful `gmail_search` with `count > 0`, the runner can force the
   `done` (via the builder from #1) on the next turn rather than waiting for ≤4 filler
   `progress` rounds. Trims ~3 wasted LLM calls and removes the stall window entirely for
   the happy path. (The nudge already exists but a 9B ignores it — a deterministic force
   is what actually converges.)

3. **(Optional) Tag the salvaged exit.** When #1 rebuilds a deliverable on a degraded
   exit, the overlay shows real data but the task is still `degraded` — consider surfacing
   a subtle "résultat récupéré après interruption" affordance so a degraded render is not
   mistaken for a clean one.

The highest-leverage change is **#1 + #2 together**: force the `done` as soon as the first
non-empty result lands, and — as a backstop for every other forced-exit path — rebuild the
structured deliverable from the salvaged tool result so the overlay is never empty when the
data exists.

## Relationship to prior work

Same control-loop weakness as [2026-05-29 mail-tool-loop](2026-05-29-mail-tool-loop.md)
(RC1 convergence on a weak model). That investigation fixed the **text** path: a forced
stall now salvages `result_summary` so Jarvis stops saying *"aucun résultat"*. This run
confirms that fix works (Jarvis spoke the mail). The remaining, now-isolated defect is the
**structured deliverable**: the salvage was intentionally text-only, so the Mail overlay
goes empty on any forced stall. Fix #1 closes that last gap.
