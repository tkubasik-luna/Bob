# Design Tokens (extraits CSS `hud.css` / `overlay.jsx`)

Tokens partagés via CSS vars du HUD existant — pas de nouveau token nécessaire pour Mail overlay :

- `--accent` : cyan HUD (sphère / route map / highlights)
- `--bg` : fond sombre
- `--hud-rule` / `--hud-rule-dim` : règles structurelles
- `--hud-fill` : remplissages blocs

Flags couleurs spécifiques email :
- `PRIORITY` flag : tonalité chaude/jaune (cf. screenshot 01-v4.png)
- Avatar gradient : `ov-avatar-grad-1` (rouge-rose dans mockup, par expéditeur)

Typo : reprend stack HUD existante (mono pour métadata, sans-serif pour subject/body — à confirmer via `frontend/src/hud.css` lors implémentation).

Pas de nouvelles élévations ni radii spécifiques — corner brackets remplacent shadow.
