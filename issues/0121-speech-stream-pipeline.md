# 0121 — SpeechStreamPipeline : TTS pipeliné + buffer d'envoi

## Parent

`prd/0018-oral-latency-reliability.md` (Module 3)

## What to build

Un objet pipeline qui possède le chemin « flux de phrases en entrée → flux de chunks PCM en sortie » :

- la phrase N+1 entre en synthèse Kokoro pendant que les chunks de la phrase N sont encore en cours d'envoi au client (suppression du gap ~250 ms entre phrases) ;
- une queue bornée producteur/consommateur sépare la synthèse de l'écriture WebSocket : un client lent applique une backpressure à la queue, jamais au synthétiseur ;
- le pipeline est annulable d'un seul appel (le barge-in coupe tout d'un coup) ;
- il pose la mark `tts_first_chunk` (0117) ;
- les événements debug par chunk audio sont remplacés par un résumé périodique (count + bytes par fenêtre).

Brancher le say-path du WS router sur ce pipeline à la place de la boucle phrase-par-phrase actuelle.

## Acceptance criteria

- [ ] Avec un fake synthétiseur et un sink lent, la synthèse de la phrase N+1 démarre avant la fin du drain de la phrase N (timestamps sous fake clock).
- [ ] La queue est bornée : un sink bloqué arrête la production à la limite, sans croissance mémoire ; la reprise du sink redraine.
- [ ] Un seul `cancel()` arrête synthèse + drain ; aucun chunk n'est envoyé après l'annulation.
- [ ] La mark `tts_first_chunk` apparaît dans le résumé 0117 du turn.
- [ ] Plus d'événement debug par chunk : un résumé batché par fenêtre, le volume d'événements sur une réplique de 10 s chute en conséquence.
- [ ] Le chemin réel (say-path WS) passe par le pipeline ; lecture audio nominale inchangée côté client.
- [ ] Tests d'isolation du pipeline (fake synth/sink) + test d'intégration du say-path.

## Blocked by

- `issues/0117-turn-latency-metrics.md`
