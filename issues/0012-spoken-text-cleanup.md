## Parent

`prd/0002-voice-mode.md`

## What to build

Enrichir `text_segmenter` (ou ajouter un module utilitaire dédié `spoken_text_cleaner` appelé en amont du `tts_service`) pour nettoyer le texte avant synthèse, afin que Bob ne lise pas le markdown brut.

Règles de nettoyage :

- Supprimer les emphases markdown inline : `**`, `*`, `_`, `~~`, `#` de début de ligne, backticks inline (` ` ` `).
- **Skip complet** des blocs de code délimités par ` ``` … ``` ` : ne rien lire à leur place, ne pas substituer par un mot. Le segmenter ne doit pas émettre de phrase pour le contenu interne d'un bloc de code.
- Convertir les puces de liste (`- `, `* `, `1. `, `2. ` …) en début de phrase plain.
- Supprimer les URLs entières (regex sur `http(s)://…`) ou les remplacer par le mot "lien" — choix à figer pendant l'implémentation et documenter dans le module.

La logique reste pure (in/out déterministe). L'affichage texte côté front n'est pas modifié — seul ce qui est envoyé au TTS l'est.

## Acceptance criteria

- [ ] Bob reçoit une réponse contenant un bloc de code triple-backtick → la voix saute totalement le bloc et enchaîne sur le texte qui suit
- [ ] Bob reçoit du texte avec `**emphase**` → la voix lit "emphase" sans prononcer les astérisques
- [ ] Bob reçoit une liste à puces → la voix lit les items en prose sans dire "tiret" ni "astérisque"
- [ ] URLs : comportement choisi (suppression ou "lien") appliqué uniformément et documenté
- [ ] Aucune régression sur la lecture des phrases en prose simple
- [ ] L'affichage texte dans le chat reste identique au markdown d'origine (le nettoyage est isolé du rendu visuel)
- [ ] Toggle voix OFF : aucun comportement modifié

## Blocked by

- `issues/0011-sentence-streaming-segmenter.md`
