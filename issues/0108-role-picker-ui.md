## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Frontend — RolePicker ; Annexes A.2, D).

## What to build

L'**UI du picker par-rôle** + l'état temps-réel dans le HUD.

- **`SettingsControl`** passe d'un switch global à une **section par-rôle** : pour chaque
  rôle (`jarvis`/`thinker`/`draft`/`subagent`), provider (Claude CLI / LM Studio) +
  `base_url` + modèle (dropdown depuis `GET /models` du host du rôle) + context length.
- **Section STT** à part (moteur whisper.cpp affiché, modèle réglable — `large-v3-turbo`
  par défaut).
- **Feedback budget** : afficher l'usage/plafond par host et l'avertissement de dépassement
  (S11) ; états `ready`/`offline` par rôle.
- **Indicateur de floor** dans le HUD : refléter `turn_state` (qui a la parole :
  idle/écoute/réfléchit/parle) à partir des events `voice`.
- Toggle voix existant → arme/désarme le micro (`voice_start`/`voice_stop`).

## Acceptance criteria

- [ ] Section par-rôle dans `SettingsControl` : provider + base_url + modèle + context length, par rôle ; `PUT` par-rôle câblé (S10).
- [ ] Dropdown modèle alimenté par les modèles du host du rôle ; section STT séparée (modèle réglable).
- [ ] Feedback budget par host + avertissement de dépassement ; badges `ready`/`offline` par rôle.
- [ ] Indicateur de floor HUD piloté par `turn_state` (idle/user_speaking/thinking/bob_speaking).
- [ ] Toggle voix arme/désarme le micro (`voice_start`/`voice_stop`).
- [ ] Tests frontend (pattern `SettingsControl.test.tsx`) : rendu par-rôle, sélection provider/modèle, dispatch `PUT` par-rôle, rendu de l'indicateur de floor sur events `turn_state`, affichage avertissement budget.

## Blocked by

- `issues/0106-per-role-selection.md`
- `issues/0107-model-budget-multiload.md`
