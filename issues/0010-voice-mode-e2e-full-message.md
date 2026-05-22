## Parent

`prd/0002-voice-mode.md`

## What to build

Câbler le mode Vocal de bout-en-bout, dans sa forme la plus simple : synthèse déclenchée **après** réception complète de la réponse LLM, sans découpage par phrase. Cette slice est le tracer bullet qui prouve que toute la chaîne fonctionne end-to-end.

Côté front : lorsque le toggle voix est ON, joindre un flag `voice: true` à chaque requête chat envoyée sur la WS. Sur réception d'events `audio_chunk`, dispatcher vers `audioPlayer.enqueue`. Sur `audio_end`, marquer le `msg_id` comme terminé. Retirer le bouton dev mock de la slice précédente (ou le laisser caché derrière un flag).

Côté backend : dans `ws_router`, lire le flag `voice` de la requête entrante. Si actif, après que le stream LLM a complètement terminé pour ce `msg_id`, prendre la réponse texte assemblée, la passer à `tts_service.synthesize`, puis émettre le PCM sur la WS — soit en un seul `audio_chunk` (si taille raisonnable) soit en quelques chunks de taille fixe — puis un `audio_end`. Étendre le discriminated union des types d'events WS côté backend et côté front (`types/ws.ts`) avec `audio_chunk`, `audio_end`.

Pas encore de nettoyage markdown (markdown brut envoyé tel quel à Kokoro, qualité dégradée acceptée pour cette slice). Pas encore d'interruption. Pas encore d'indicateur visuel sur la bulle.

## Acceptance criteria

- [ ] Types WS `audio_chunk` et `audio_end` ajoutés côté backend et côté front, type-safe des deux côtés
- [ ] Front envoie `voice: true` dans la requête chat si toggle ON, sinon n'envoie pas le flag (ou `false`)
- [ ] Backend synthétise uniquement si flag `voice` actif dans la requête entrante
- [ ] Avec toggle ON, message simple type "Dis-moi bonjour en une phrase" → réponse texte affichée normalement **et** Bob lit la réponse à voix haute
- [ ] Avec toggle OFF, aucun event audio n'est émis ni reçu, comportement texte inchangé
- [ ] Réponse texte continue de s'afficher pendant que l'audio joue (l'un n'écrase pas l'autre)
- [ ] La WS existante reste utilisable en parallèle (pas de blocage des events texte par la synthèse)
- [ ] Compatibilité avec les composants server-driven UI existants : si la réponse contient des composants riches, ils s'affichent ; seule la portion texte est lue

## Blocked by

- `issues/0008-tts-kokoro-bootstrap.md`
- `issues/0009-audio-player-toggle-ui.md`
