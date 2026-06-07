## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Penser en parallèle — Draft ; Annexes A.2, F).

## What to build

L'**anticipation** : le Draft spéculatif pré-rédige la réponse pendant que l'utilisateur
parle, pour une réponse quasi-instantanée à l'endpoint.

- **`SpeculativeDraft`** : rôle `draft` (mini modèle rapide, lu depuis la sélection S10),
  génère sur le transcript **partiel** un **texte de réponse brut hors codec** (pas un
  tool-call validé). Ne spécule **que la réponse conversationnelle**, pas les tours qui
  dispatchent un outil (ceux-là retombent en froid).
- **Gate de commit** à l'endpoint : **fast-path préfixe** (transcript final ≈ préfixe du
  partiel utilisé → commit instantané) ; sinon **garde de similarité légère**
  (token-overlap/embedding) au-dessus d'un seuil → commit ; sinon → **jeté + regénération
  froide** par le Speaker.
- Le texte committé est **réinjecté dans le say-path normal** (validation triviale car déjà
  du texte) → TTS.
- Émet `draft_status {turn_id, state: drafting|ready|committed|discarded, reason?, ts}`
  (Annexe A.2) + marks `t_draft_ready` / `t_commit_decision` (Annexe F).

## Acceptance criteria

- [ ] `SpeculativeDraft` génère un texte brut hors codec sur le partiel (rôle `draft`, mini modèle) ; uniquement pour la réponse conversationnelle (un tour outil ⇒ froid, pas de draft).
- [ ] Gate de commit : fast-path préfixe (commit instantané) ; sinon garde de similarité ; sinon discard + regénération froide.
- [ ] Le texte committé entre dans le say-path normal → TTS.
- [ ] Events `draft_status` (drafting/ready/committed/discarded + `reason`) émis ; marks latence émis.
- [ ] `endpoint_to_first_audio_ms` sur un commit aligné **< 800 ms** ; `draft_hit` reporté.
- [ ] Dégradation (Annexe G) : modèle `draft` indisponible → anticipation désactivée, le reste marche (toujours froid).
- [ ] Scénario `bob attest` (deux cas) : (a) input aligné avec le partiel → `draft_status:committed` + `latency_lt_ms endpoint→first_audio max:800` ; (b) input divergent en fin de phrase → `draft_status:discarded` puis réponse froide correcte.
- [ ] Tests unit : gate de commit (`SpeculativeDraft`) — fast-path préfixe, garde de similarité (au-dessus/en-dessous du seuil), divergence ⇒ discard.

## Blocked by

- `issues/0102-thinker-livestate-provider.md`
- `issues/0103-semantic-endpoint.md`
