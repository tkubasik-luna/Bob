## Parent

prd/0005-debug-view.md

## What to build

Permettre au développeur de passer du résumé d'un event à son détail complet sans changer de pane, et de suivre visuellement la trace complète d'un user turn en un click.

Périmètre frontend uniquement (aucun changement backend) :

- Refactor / nouveau composant `frontend/src/components/debug/DebugRow.tsx` qui rend une ligne d'event. Maintient son propre state local `expanded: boolean` (défaut `false`).
- Click n'importe où sur la ligne (sauf sur le chip `turn_id`, voir ci-dessous) → toggle `expanded`. Quand `expanded = true`, la ligne devient haute et affiche en dessous du summary :
  - Le `payload` JSON pretty-printed (indent 2 spaces).
  - Le `source` complet, le `correlation_id` (si présent), le `turn_id` complet.
  - Idéalement un syntax highlighting basique (keys, strings, numbers, booleans) — libre d'utiliser une lib comme `react-json-view` ou un mini renderer maison. Si lib non installée et coût > 5 min, fallback `<pre>` avec couleurs CSS manuelles sur tokens.
- Sur chaque ligne, à côté du summary (ou en fin de ligne), afficher un petit chip cliquable avec le `turn_id` tronqué (ex: 6 premiers chars hex). Chaque turn_id produit une couleur déterministe (hash → HSL hue, par ex), pour que les chips d'un même turn aient la même couleur dans le feed.
- Click sur le chip `turn_id` (pas sur la ligne) → highlight visuellement toutes les lignes partageant ce `turn_id` (outline coloré ou background nuancé) pendant ~5 secondes, puis retour à l'état normal. Cliquer sur un autre `turn_id` chip pendant ce highlight switche le highlight au nouveau turn.
- Le click sur le chip ne doit pas déclencher l'expand de la ligne (stopPropagation).
- Le pretty-print JSON pour les payloads LLM (qui peuvent contenir un `messages` array de plusieurs dizaines de KB) doit rester performant — ne pas re-render à chaque tick, mémoiser le `JSON.stringify` (`useMemo` ou équivalent).
- Le scroll position du feed doit rester stable quand une ligne est expand : l'expand pousse les lignes suivantes vers le bas, ce qui est attendu.

## Acceptance criteria

- [ ] Click sur une ligne d'event ouvre un bloc inline juste en dessous montrant le `payload` JSON pretty-printed, le `source`, le `turn_id` complet, et le `correlation_id` si présent.
- [ ] Click à nouveau sur la ligne (ou sur la zone expand) ferme le bloc.
- [ ] Le JSON pretty-printed est lisible : indentation 2 spaces, retours ligne sur arrays/objects, pas de tronquage muet sur les longs strings.
- [ ] Un payload LLM avec un `messages` array de ~30 messages s'affiche correctement sans figer le UI.
- [ ] Chaque ligne montre un chip `turn_id` court (6 chars) coloré.
- [ ] Deux events partageant le même `turn_id` ont des chips de la même couleur.
- [ ] Click sur un chip `turn_id` highlight toutes les autres lignes du même turn (outline ou background nuancé) pendant ~5 secondes.
- [ ] Le click sur le chip `turn_id` ne déclenche pas l'expand de la ligne.
- [ ] Le highlight `turn_id` survit au scroll : si je scroll up pour voir des events anciens du même turn, ils sont aussi mis en évidence (sauf s'ils ont quitté la fenêtre virtualisée — pas de virtualisation v1, donc OK).
- [ ] Aucune régression sur la toolbar (filtres catégorie/severity continuent de fonctionner) ni sur l'affichage des couleurs de severity.

## Blocked by

issues/0039-debug-view-instrumentation.md
