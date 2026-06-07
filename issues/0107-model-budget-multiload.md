## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Modèles & picker — budget ; Annexe J).

## What to build

Le **budget mémoire multi-modèles** : permettre des modèles résidents distincts par rôle
(vraie concurrence) sans OOM, en remplaçant la politique offload-first.

- **`ModelBudget`** : module **pur** — footprint d'un modèle = taille fichier disque
  (GGUF/MLX) + marge KV-cache (∝ `context_length`) ; plafond **par host** : local = RAM
  détectée (sysctl) − `reserve_gib` (~8) ; distant = `per_host_override` si renseigné,
  sinon **skip** (try+catch OOM). Fit-check : somme des footprints résidents ≤ plafond.
- **`LMStudioManager` v2** : remplace **offload-first** (`load()` actuel évince tout) par un
  **multi-load budget-aware + offload sélectif ref-compté** — charge les modèles des rôles,
  les garde résidents, n'évince un modèle que lorsque **plus aucun rôle ne le référence**
  (sur le même host), **refuse + avertit** si le budget serait dépassé. Manager **par host**.
- **Séquence de boot/(re)chargement** (Annexe J) : grouper les rôles par host, budget-check,
  load sélectif, marquer `ready`/`offline`.

⚠️ Revient en partie sur la décision robustesse offload-first (2026-06-05) ; le garde-fou
`ModelBudget` est ce qui rend la réversion sûre. Les tests existants
(`test_lm_studio_manager.py`) encodent l'ancien comportement et **doivent évoluer**.

## Acceptance criteria

- [ ] `ModelBudget` pur : footprint (taille disque + marge KV ∝ ctx) ; plafond par host (local détecté − reserve ; distant override/skip) ; fit-check somme ≤ plafond.
- [ ] `LMStudioManager` v2 : multi-load (plusieurs modèles résidents), **offload sélectif ref-compté** (n'évince que les non-référencés sur le host), **refus + avertissement** si dépassement budget AVANT load.
- [ ] Sélectionner un modèle déjà chargé pour un rôle ne décharge pas inutilement les autres (ref-count).
- [ ] Manager par host ; boot suit la séquence Annexe J.
- [ ] Dégradation (Annexe G) : OOM réel au load malgré budget OK → garder l'état précédent, refuser ce swap, **jamais** 0 modèle pour un rôle actif ; host distant injoignable → rôle `offline`.
- [ ] Tests existants `test_lm_studio_manager.py` migrés du comportement offload-first vers multi-load.
- [ ] Scénario `bob attest` : assigner deux rôles à deux modèles locaux distincts → assert les deux résidents (concurrence) ; assigner un 3e qui dépasse le plafond → assert refus + message.
- [ ] Tests unit : `ModelBudget` (footprint, fit, plafond local/distant, marges) ; `LMStudioManager` v2 (multi-load, offload ref-compté, refus budget, ré-sélection sans offload).

## Blocked by

- `issues/0106-per-role-selection.md`
