## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (section « Harnais d'attestation agent » + Annexe C).

## What to build

Le **squelette du harnais d'attestation `bob` CLI** (greenfield — aucun CLI aujourd'hui).
Driver headless **black-box sur le vrai WS/HTTP** qui asserte sur le ring buffer
`/ws/debug` (`event_bus_v2` / `DebugEvent`). Le slice rend tout le reste **attestable**.

Composants :
- **`bob` CLI** : console_script via `pyproject`, sous-commandes `attest` / `say` / `scenario`.
- **`EphemeralBackend`** : boote un backend **isolé** (BOB_DATA_DIR temporaire, DB fraîche,
  port dédié), exécute, tear-down — zéro pollution de l'état réel.
- **`FakeLlmBackend`** : client LLM **scriptable déterministe**, branché via le switch
  provider de la factory (nouveau provider `fake`). Réponses pilotées par le scénario
  (par rôle + `on_input_contains`). Réutilise le pattern de fake SDK des tests existants.
- **`ScenarioRunner`** : parse le YAML (Annexe C), exécute la timeline (`inject_text`,
  `wait_state`, `wait_event`, `wait_ms`), capture les events `/ws/debug`, applique les
  assertions.
- **`AttestAssertions`** : moteur extensible ; sous-ensemble minimal ici
  (`event_emitted`, `no_error_events`, `deliverable_nonempty`).
- **Verdict JSON** (Annexe C) sur stdout + **exit code** `0`/`1`.

Atteste d'abord le **path texte EXISTANT** de Bob (prouve que le harnais fonctionne
contre le Bob actuel, avant tout ajout temps-réel).

## Acceptance criteria

- [ ] `bob attest <scenario.yaml>` boote un backend éphémère isolé (BOB_DATA_DIR temp, port dédié) et tear-down proprement, sans toucher l'état réel (thread Jarvis, tasks).
- [ ] Provider `fake` sélectionnable via la factory ; `FakeLlmBackend` rend des réponses scriptées par le scénario (clé `fake_llm`, par `role` + `on_input_contains`).
- [ ] Mode `--text` : `inject_text` passe par le path `client_text` existant ; le runner capture le flux `/ws/debug`.
- [ ] Assertions `event_emitted`, `no_error_events`, `deliverable_nonempty` implémentées ; moteur extensible (les autres `kind` arriveront avec leurs slices).
- [ ] Verdict JSON conforme au schéma Annexe C (`scenario`, `ok`, `assertions[]`, `events_captured`, `backend`, `llm`) sur stdout.
- [ ] Exit code `0` si `ok:true`, `1` sinon.
- [ ] Scénario de démo `--text` : un tour texte → assert `say` émis + `deliverable_nonempty` PASSE (atteste le Bob actuel) ; un scénario volontairement faux ÉCHOUE avec exit `1`.
- [ ] Tests unit : `ScenarioRunner` (parse + timeline), `AttestAssertions` (chaque `kind`), `EphemeralBackend` (boot/teardown + isolation), `FakeLlmBackend` (réponses scriptées déterministes).

## Blocked by

- None - can start immediately
