## Parent

`prd/0002-voice-mode.md`

## What to build

Finir l'UX du mode Vocal : indicateur visuel "Bob parle" sur la bulle en cours de lecture, retours utilisateur pendant le téléchargement initial du modèle, et gestion d'erreur TTS avec fallback propre.

Indicateur de lecture :

- Ajouter un champ `speakingMsgId: string | null` dans `chatStore`, mis à jour par `audioPlayer` via le mécanisme d'observation (callback / event).
- Dans `ChatMessageBlock`, afficher une petite icône onde sonore animée tant que `speakingMsgId === message.id`. L'icône disparaît dès la fin de la lecture (ou interruption).

Téléchargement initial Kokoro :

- Lors du premier envoi en mode Vocal, si `model_downloader` doit télécharger, afficher un toast info ("Préparation de la voix…") ou un état header dédié, et masquer/débloquer à la fin du download.

Erreurs TTS :

- Si `model_downloader` échoue (réseau, disque) ou si `tts_service.synthesize` lève une exception, le backend émet un event d'erreur (réutilisation du canal d'erreur existant ou nouvel `audio_error`).
- Le front affiche un toast "TTS indisponible : <raison>" via le composant `Toast` existant.
- La réponse texte continue de s'afficher normalement.
- Le toggle voix **reste ON** — la prochaine réponse retentera la synthèse.

## Acceptance criteria

- [ ] `speakingMsgId` exposé par `audioPlayer` vers `chatStore`, mis à jour à chaque transition idle ↔ speaking
- [ ] Icône onde sonore animée visible sur la bulle exacte en cours de lecture, et uniquement celle-là
- [ ] Icône disparaît à la fin naturelle de la lecture **et** sur interruption (issue 0013)
- [ ] Premier message en mode Vocal sans modèle local → toast "Préparation de la voix" visible, lecture démarre après download
- [ ] Coupure réseau pendant download initial → toast erreur, réponse texte affichée, toggle voix reste ON
- [ ] Crash forcé de `tts_service` (test manuel : voix invalide, payload tordu) → toast erreur, fallback texte, application stable
- [ ] Aucune fuite : un message lu jusqu'au bout ne laisse pas l'icône animée résiduelle sur d'anciennes bulles

## Blocked by

- `issues/0010-voice-mode-e2e-full-message.md`
