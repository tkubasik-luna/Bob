# Investigation — Mail sub-agent loops despite a successful tool result

**Date:** 2026-05-29
**Reporter:** AFK request — "il a récupéré le mail via le tool, mais il a bouclé les calls et fail à la fin"
**Provider/model:** `lm_studio` · `qwen/qwen3.5-9b` (small 9B local model), guided JSON ON
**Logs:** `backend/logs/orchestration.jsonl`, `backend/logs/llm-2026-05-29.jsonl`

## TL;DR

The Gmail tool worked **on the very first call** — it returned the exact email the
user wanted. The sub-agent then **failed to terminate**: instead of emitting `done`
with the Mail payload, it spent 34 iterations emitting filler `progress` and
re-issuing near-identical `gmail_search` calls until the **token cap** tripped. The
cap path emits `done(degraded, token_cap)` with **`result_summary=""` and
`ui_payload=None`** — so every piece of retrieved data is **discarded**, and Jarvis
tells the user *"aucun résultat"* even though the email was in context the whole time.

Root cause is **not** the tool and **not** the transport. It is the **sub-agent
control loop**: nothing forces convergence, and the cap exit throws away what the
agent already found.

## Timeline (task "Recherche mail Olivier Berni", 07:10:07–07:11:35)

| time | event |
|------|-------|
| 07:10:07 | User: *"je veux que tu me sorte le mail sur l'interessement d'olivier berni"* |
| 07:10:19 | progress: "recherche Gmail" |
| 07:10:21 | **tool_call `gmail_search {subject_contains:"intéressement"}` → ok, count:5** |
| | result includes **Olivier Berni `<olivier@lunabee.com>`, subject "👋 Intéressement 2025 !", full bodyPreview** — the exact target |
| 07:10:24 → 07:11:32 | **~50 `progress` + ~29 redundant `gmail_search`, never a `done`** |
| 07:11:32 | runner forces `done(status=degraded, reason_code=token_cap)` — `result:""`, 34 iterations, 203 455 tok, 83 s |
| 07:11:35 | Jarvis: *"La recherche sur les mails d'Olivier Berni n'a donné aucun résultat."* |

A second attempt at 07:12 (user replans: *"c'est un mail envoyé par olivier sur l'interessement"*) loops the same way — one good `gmail_search`, then ~40 **identical** progress lines (*"La recherche par sujet était trop restrictive…"*) before terminating.

## Evidence

**The first tool result already held the answer** (`tool`-role message in the transcript):
```json
{"tool":"gmail_search","status":"ok","result":{"query":"subject:\"intéressement\"","count":5,
 "messages":[{"from":{"name":"Olivier Berni","email":"olivier@lunabee.com"},
   "subject":"👋 Intéressement 2025 ! À lire et à répondre 🙏",
   "bodyPreview":"Hello Tom 👋 … Conformément à l'accord d'intéressement en vigueur chez Lunabee Studio…"}, …]}}
```

**Action mix over the loop (parsed from `raw_response`):** `progress×50`, `tool_call×29`, `done×0` (until the cap forced one). The model never voluntarily emitted `done`.

**`gmail_search` args issued (deduped):**
| count | args |
|------:|------|
| 12 | `{"max_results":5,"subject_contains":"intressement"}` ← **"é" mangled to control char `` (DC3)** |
| 10 | `{"max_results":5,"subject_contains":"intéressement"}` |
| 6 | `{"from_name":"Olivier Berni","max_results":5}` |
| 1 | `{"from_name":"Olivier Berni","max_results":5,"subject_contains":"intéressement"}` |

The Gmail skill pack explicitly says *"Ne retente JAMAIS deux fois le même appel"* and *"Une fois un résultat NON VIDE reçu … termine avec un `done`"* — both ignored by the 9B model.

## Root causes (priority order)

### RC1 — No convergence forcing function *(primary)*
`SubAgentRunner._run` (`backend/src/bob/sub_agent/runner.py:586`) loops on `progress`/`tool_call`
and only exits when the **model** emits `done` or a hard cap fires. There is:
- no nudge after a successful tool result ("you have output — emit `done` now");
- no cap on consecutive `progress` actions;
- no detection of repeated/identical `tool_call`s.

A weak local model (qwen3.5-9b) does not reliably self-terminate, despite the prompt
instructing it to. The loop is structurally unbounded except by the backstop caps.

### RC2 — Cap exits discard retrieved data *(makes it fatal/user-visible)*
On any cap, the runner calls `_emit_terminal_done(..., result_summary="", ...)`
(`runner.py:631-643` → `_emit_terminal_done` at `:1528`, which passes `ui_payload=None`).
The degraded `done` carries **nothing**, so the email already sitting in the transcript
is thrown away and Jarvis says *"aucun résultat."* Contrast the parse-exhaustion path,
which already salvages content via `_salvage_display` (`runner.py:770`). The cap paths
have no equivalent salvage.

### RC3 — `token_cap` is the first backstop to fire *(context, by design)*
`tokens_used += _estimate_messages_tokens(messages) + …` (`runner.py:738`) re-counts the
**entire growing transcript every iteration** (the policy docstring intends "aggregate
token spend across LLM calls"). Cumulative is O(n²): ~411 k counted for ~3.6 k of unique
content this run. Consequence: `token_cap` (200 k) trips around iteration ~34, *before*
`iteration_cap` (50). So the loop is effectively iteration-bounded, and the user-facing
symptom is a `token_cap` degraded-done. Not a bug on its own, but it is why the empty
`done` is tagged `token_cap`.

### RC4 — No tool-call dedup *(contributing)*
~29 `gmail_search` calls, ~22 of them duplicates of two arg-sets. Each duplicate burns an
iteration and re-grows the transcript. Nothing rejects or short-circuits a repeat call.

### RC5 — Guided-decode mangles multibyte UTF-8 *(contributing)*
Under LM Studio `response_format: {type: json_schema}` constrained decoding
(`llm_client.py:354`), `"intéressement"` was emitted as `"intressement"` (é →
U+0013) in **12** calls. Those corrupted queries match nothing → `count:0` → reinforce the
model's "no result" belief and trigger more retries. 9B model + byte-level grammar on a
multibyte char.

## Recommended fixes

1. **Forcing function (RC1).** After a tool result, on the next iteration inject a
   `system_validator` nudge ("you now have a tool result above; if it answers the goal,
   emit `done`"). And/or cap consecutive `progress`-without-`tool_call`/`done` (e.g. 2)
   and force a `done` afterwards.
2. **Salvage on cap (RC2).** When a cap fires, scan the transcript for the last successful
   `tool` result (or last assistant content) and put it in `result_summary` / reconstruct
   a Mail `ui_payload`, emitting `done(degraded, …)` with real content instead of empty.
   Mirror `_salvage_display`.
3. **Dedup tool calls (RC4).** Track `(name, args)` seen this run; on a repeat, return a
   `system_validator` correction ("you already called X with these args — the result is
   above; use it or change the args / emit `done`") rather than re-dispatching.
4. **UTF-8 integrity (RC5).** Normalise/validate `args` strings post-decode (reject
   control chars), or relax guided decoding for the tool-arg leg; consider a larger model.

The single highest-leverage change is **#1 + #2 together**: force convergence after the
first usable tool result, and never exit a cap with an empty payload when data exists.

## Fix implemented (2026-05-29)

All in `backend/src/bob/sub_agent/runner.py` (+ a new reason code), tool-agnostic so the
runner stays generic. New tests in `backend/tests/test_sub_agent_v2_runner.py`.

- **RC1 — stall forcing function.** New per-run `stall_count`: a `progress` emitted *after*
  a successful tool result, or a duplicate `tool_call`, increments it; a fresh successful
  result resets it. At `_STALL_NUDGE_THRESHOLD` (2) the runner injects a `system_validator`
  nudge ("you already have a tool result — emit `done` now"); at `_STALL_FORCE_THRESHOLD`
  (4) it force-terminates with a salvaged `done(degraded, stalled_no_progress)`. Threshold 2
  leaves room for the recipe's single legitimate "lecture du mail" reflection.
- **RC2 — salvage on cap/stall.** `_salvage_tool_result_text()` folds the last successful
  tool result into the degraded `done`'s `result_summary`, so the `iteration_cap` /
  `token_cap` / `stalled` exits surface the retrieved data instead of `""`. Jarvis can now
  answer from it instead of saying "aucun résultat".
- **RC4 — tool-call dedup.** `seen_tool_calls` keys on `_tool_call_key(name, args)`; an
  identical repeat is suppressed pre-dispatch (no re-run, no transcript bloat), nudged, and
  counted toward the stall guard. The ~22 duplicate `gmail_search` dispatches from the log
  collapse to 1.
- **RC5 — control-char arg guard.** `_validate_tool_args` rejects string args carrying C0/C1
  control chars (the `é`→U+0013 mangle) → `invalid_args` → existing bounded
  `system_validator` retry; the corrupted query never dispatches.
- **New reason code** `REASON_STALLED = "stalled_no_progress"` (`bob/validation/reason_codes.py`,
  append-only, schema version unchanged); frontend `reason_codes.ts` regenerated.
- **Privacy (issue 0056 posture preserved).** The salvaged text can embed raw tool output
  (an email `bodyPreview`), so its DEBUG mirrors are scrubbed: `_finalize_done` and
  `_emit_task_message` take `redact_result_in_debug` / `redact_content_in_debug`, emitting a
  redacted copy to the ring buffer / `/ws/debug` / JSONL sink while the chat client still
  receives the full `task.result`. Covered by a dedicated test.

Not changed: the token accounting (RC3) — it counts cumulative prompt processing *by design*
and now rarely fires since the loop converges first. Known limitation: dedup blocks any
identical `(name, args)` repeat, which is correct for every current tool (search/fetch) but
would need revisiting if a pollable/idempotent-retry tool is ever added.

Checks: `ruff check` / `ruff format` / `mypy` clean on changed files; 6 new tests pass; full
suite 825 passed (the one failure, `test_config::test_settings_loads_from_env`, is a
pre-existing stale default unrelated to this change).

## Follow-up — Trou A/B (2026-05-29, second task "Dernier mail du jour")

A SECOND task surfaced the same class of hang via a path the first fix did NOT cover.
Task `2deaade5…`, goal *"Trouver et afficher le dernier e-mail reçu aujourd'hui"*: the
sub-agent issued **one** `gmail_search {label:"INBOX", max_results:1, after:"today"}`,
which **errored** — `gmail_search_invalid_query: after must be a YYYY-MM-DD string … got
'today'` (the model passed the literal `"today"`). It then emitted **23 consecutive
`progress`** lines (narrating a call it never re-issued) and was **hard-killed** at ~91 s
with an empty result → Jarvis "a échoué. Raison brute : ''".

Why RC1 missed it: the stall guard only armed once `last_tool_result is not None`, set
ONLY on a SUCCESSFUL dispatch. The single tool call **errored**, so `last_tool_result`
stayed `None`, `stall_count` never incremented, and the progress loop was unbounded
(0 `system_validator` nudges across all 26 calls; runner caps are 50 iters / 1800 s /
200 k tok — none reached before the external kill).

Two gaps fixed (all in `runner.py`; reason code unchanged — reuses `REASON_STALLED`):

- **Trou A — `progress` always counts.** A `progress` now increments `stall_count`
  regardless of whether a tool result exists; only a fresh SUCCESSFUL result (or a `done`)
  resets it. Empirically (logs 21–29 May) no task reaching a terminal `done` emitted >3
  consecutive `progress` — every run with ≥4 was a loop — so force-at-4 never truncates a
  legitimate task. Pure-progress spin now ends `done(failed, stalled)` at iter 4.
- **Trou B — errored dispatch counts + feeds the nudge.** A handler-level tool error
  (`status=error`, e.g. `after:"today"`) is no longer a silent `tool` message: it
  increments `stall_count`, arms a context-aware nudge naming the error ("ton appel a
  échoué : … corrige les arguments et réessaie UNE fois, ou `done(failed)`"), and on force
  emits `done(failed, stalled)` whose `result_summary` names the error. A new opt-in
  `persist_result_on_failure` on `_finalize_done` writes that summary to `task.result`
  (the only field the orchestrator's *failed*-synthesis reads) so Jarvis explains the
  failure instead of an empty "Raison brute". Every other failure path is byte-identical.
- **Convergence helpers.** `_force_stalled_done` (salvage success → degraded / name error →
  failed / empty → failed) and `_stall_nudge_message` (3 context variants) dedupe the three
  stall sites (progress / duplicate tool_call / errored dispatch).

Replay of `2deaade5…` under the fix: iter1 tool error (stall 1) → iter2 progress (stall 2,
nudge naming the date error) → iter4 force `done(failed, stalled)` carrying "…after must be
a YYYY-MM-DD…". Bounded at 4 iters vs 25 + hard-kill; Jarvis can now say *why* it failed.

Not fixed here (separate foot-gun, "Trou C"): the Gmail skill pack lists `after`/`before`
without a format hint or today's date, so the weak model guessed `"today"`. Cheapest real
fix is to accept relative literals (`today`/`yesterday`) in `query_builder` or inject the
current date into the recipe — deferred.

Checks: `ruff` / `mypy` clean; 3 new tests (`test_progress_spam_without_any_tool_forces_failed_stalled`,
`test_tool_error_then_progress_spam_forces_failed_naming_the_error`,
`test_tool_error_then_successful_retry_resets_stall`) pass; the stale
`test_sub_agent_runner::test_progress_cap_exceeded` updated to pin the iteration cap below
the stall threshold; full suite 828 passed (same lone pre-existing `test_config` failure).
