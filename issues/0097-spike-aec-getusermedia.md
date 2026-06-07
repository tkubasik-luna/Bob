## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Annexe I — critères d'acceptation du spike).

## What to build

Spike de dérisquage de la **capture micro + AEC dans le webview Tauri v2 (WKWebView
macOS)**, en mode **AFK auto-fallback** (pas de gate humain). Implémenter le chemin
webview-natif `getUserMedia({ audio: { echoCancellation: true } })`, le mesurer contre les
critères Annexe I, et **si un critère échoue, basculer automatiquement** sur le fallback
Rust (`cpal` + `webrtc-audio-processing`, capture + lecture + AEC en Rust, webview = UI).

Le slice produit un **artefact de décision** (`webview` | `rust`) exposé
programmatiquement et consommé par S3 (`0099`). Travail concret : ajouter
`NSMicrophoneUsageDescription` au bundle macOS, câbler le délégué de permission média
WKWebView (plugin/patch Tauri si requis), et fournir un harnais de mesure reproductible
(fixture WAV jouée pendant la capture → calcul de l'atténuation d'écho en dB +
transcription du mot prononcé par-dessus).

## Acceptance criteria

- [ ] `getUserMedia({audio:{echoCancellation:true}})` renvoie un `MediaStream` actif dans le build Tauri cible (entitlement + délégué de permission câblés).
- [ ] Test fixture reproductible : en jouant un TTS connu pendant la capture, l'atténuation de l'écho dans le micro est mesurée **≥ 25 dB**.
- [ ] Un mot prononcé par-dessus le TTS est transcrit correctement (sanity AEC).
- [ ] **Auto-fallback** : si un critère échoue, le slice bascule sur le chemin Rust (`cpal` + `webrtc-audio-processing`) sans intervention humaine, et le documente.
- [ ] Le **chemin retenu** (`webview` | `rust`) est persisté/exposé programmatiquement (module config ou fichier de décision) pour que S3 sache quelle source de capture câbler.
- [ ] Le mode dégradé runtime (half-duplex gate : mute mic pendant `bob_speaking`) est spécifié comme filet si l'AEC échoue plus tard (consommé par S5).
- [ ] Verdict du spike (pass/fail par critère + chemin retenu) émis en JSON.
- [ ] Tests : calcul d'atténuation dB sur fixture ; sélecteur de chemin (critères → `webview`/`rust`).

## Blocked by

- None - can start immediately
