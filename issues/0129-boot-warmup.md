# 0129 — BootWarmup : premier turn aussi rapide que les suivants

## Parent

`prd/0018-oral-latency-reliability.md` (Module 8)

## What to build

Déplacer tous les préchauffages dans une tâche de fond supervisée (0124) démarrée juste après le yield du lifespan FastAPI : moteur STT (download + load whisper), moteur TTS (preload + warmup Kokoro), et clients LLM par rôle (Jarvis, Thinker, Draft). Les clients WS se connectent immédiatement — le boot ne bloque plus 30 s+. Un turn vocal qui arrive pendant le warmup n'attend que ce qui n'est pas encore prêt, avec les toasts « preparing » existants. Échecs de warmup : log fort + reflet dans l'état de santé. Le setting skip-preload existant reste honoré (warmup no-op).

## Acceptance criteria

- [ ] Le lifespan yield sans attendre STT/TTS/role-clients ; une connexion WS aboutit pendant que le warmup tourne (fake engines lents).
- [ ] Un `voice_start` pendant le warmup émet les toasts « preparing » existants puis aboutit quand les moteurs sont prêts.
- [ ] Après warmup complet, le premier turn vocal ne paie aucun chargement (mesurable via 0117 : premier turn ≤ 1.5× régime établi).
- [ ] Un échec de warmup (fake engine qui lève) est loggé fort et visible dans l'endpoint de santé ; le boot continue.
- [ ] Le setting skip-preload existant désactive le warmup.
- [ ] Tests : ordre boot/connexion/warmup sous fakes ; turn pendant warmup ; échec de warmup.

## Blocked by

- `issues/0124-task-supervisor.md`
