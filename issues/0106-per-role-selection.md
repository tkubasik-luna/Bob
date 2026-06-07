## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Modèles & picker par-rôle ; Annexe D).

## What to build

La **sélection LLM par-rôle** : faire évoluer la sélection globale unique vers une **map
`{role: LLMSelection}`**, chaque rôle choisissant **Claude OU LM Studio** avec son propre
`base_url`.

- **`RoleSelectionStore`** : fichier JSON `llm_selection.json` **`schema_version:2`**
  (Annexe D) — `roles{jarvis,thinker,draft,subagent}`, `stt{engine,model}`,
  `budget{ceiling_gib,reserve_gib,per_host_override}`. Décodage défensif conservé.
- **Migration 1→2** : l'ancien shape plat seed **les 4 rôles** avec la même valeur ; `stt`
  + `budget` prennent les défauts ; `ceiling_gib:null` ⇒ détecté (S11).
- **Factory par-rôle** : `build_<role>_client` épingle provider/base_url/model du rôle ;
  `LMStudioClient` **route par paramètre `model`** vers le serveur du rôle.
- **`llm_swap`** : rebuild **uniquement** le client du rôle modifié (vs les deux clients
  aujourd'hui).
- **`llm_router`** : endpoints `GET`/`PUT` de sélection **par rôle**.

Attestable sur les rôles **existants** (`jarvis`, `subagent`) — `thinker`/`draft` se
branchent quand S6/S8 les consomment.

## Acceptance criteria

- [ ] `RoleSelectionStore` lit/écrit le JSON `schema_version:2` (Annexe D) ; décodage défensif (clé manquante/typée faux → défaut).
- [ ] Migration 1→2 : un ancien `llm_selection.json` plat seed les 4 rôles à l'identique + défauts `stt`/`budget`.
- [ ] Chaque rôle porte `{provider (claude_cli|lm_studio), base_url, lm_model, context_length}` ; `base_url` **par-rôle** (serveurs différents possibles).
- [ ] `build_<role>_client` construit le client du rôle ; `LMStudioClient` envoie le bon `model` par requête.
- [ ] `PUT` d'un rôle rebuild **seulement** ce rôle ; `GET` renvoie la map par-rôle.
- [ ] Scénario `bob attest` : configurer `jarvis=lm_studio:modelA` et `subagent=claude_cli` puis un tour → assert `role_used_model role:jarvis model:modelA`.
- [ ] Tests unit : `RoleSelectionStore` (round-trip v2, **migration 1→2**, fichier corrompu/partiel → défauts) ; factory par-rôle (provider/base_url/model corrects par rôle).

## Blocked by

- `issues/0098-attest-harness-skeleton.md`
