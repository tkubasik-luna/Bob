## Parent

`prd/0002-voice-mode.md`

## What to build

Remplacer la synthèse "réponse complète" par une synthèse **phrase par phrase pendant le stream LLM**, pour réduire fortement la latence du premier son. Introduire le deep module `text_segmenter` : un générateur async qui consomme les deltas LLM token par token et yield une phrase complète dès qu'il détecte une frontière (`.`, `!`, `?`, `\n\n`). À la fin du stream LLM, flusher tout reliquat comme dernière phrase.

Le segmenter est pure logic (sans I/O, sans dépendance Kokoro), déterministe, isolé. Pas encore de nettoyage markdown / code blocks à cette étape — la phrase complète textuelle est envoyée à `tts_service` telle quelle.

Adapter `chat_service` / `ws_router` pour brancher le pipeline : delta LLM → segmenter → (dès qu'une phrase est prête) → `tts_service.synthesize` → `audio_chunk` WS. Plusieurs synthèses peuvent être pipelinées (la phrase N+1 peut commencer à être synthétisée pendant que la phrase N est en cours de lecture front).

## Acceptance criteria

- [ ] Module `text_segmenter` créé, pure logic, sans dépendance externe
- [ ] Génère une phrase dès rencontre d'une frontière `.`, `!`, `?` (suivie d'un espace, fin de stream ou retour ligne) ou `\n\n`
- [ ] Flush du reliquat à la fin du stream LLM (dernière phrase sans ponctuation finale)
- [ ] Pipeline branché : pour un message du type "Compte de 1 à 5 en faisant une phrase par chiffre", Bob commence à parler avant que le LLM ait fini de générer
- [ ] Latence du premier son après réception du premier token LLM : < 2 s en local Apple Silicon (validation manuelle)
- [ ] La lecture audio reste continue entre phrases (pas de gap audible, l'`audioPlayer` enchaîne)
- [ ] Comportement texte (affichage progressif des deltas) inchangé
- [ ] Toggle voix OFF : segmenter et TTS ne sont pas appelés du tout

## Blocked by

- `issues/0010-voice-mode-e2e-full-message.md`
