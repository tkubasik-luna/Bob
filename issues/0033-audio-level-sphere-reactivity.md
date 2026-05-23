## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Audio reactivity réelle : la sphère pulse avec le RMS de l'audio TTS sortant.

Modifier `frontend/src/audio/audioPlayer.ts` pour exposer un `AnalyserNode` greffé sur la chaîne Web Audio existante. Patch minimal du graphe : `source → analyser → destination`. Exposer `getAnalyser(): AnalyserNode | null` (retourne `null` si l'AudioContext n'est pas encore initialisé). Si plusieurs consommateurs ont besoin du level, partager le même AnalyserNode (singleton lazy-init).

Créer `frontend/src/sphere/useAudioLevel.ts`. Hook qui :
- Au mount, demande `audioPlayer.getAnalyser()`. Si `null`, polling léger (`setTimeout` 200ms) jusqu'à dispo, OU réessaye quand `speakingMsgId` change (préférer la 2e approche : moins de polling).
- Quand AnalyserNode dispo, ouvre une boucle `requestAnimationFrame` qui appelle `getByteTimeDomainData(buf)` chaque frame, calcule le RMS normalisé (0..1), stocke dans un `useRef<number>(0)`.
- Retourne le ref (pas state — éviter re-render à 60fps).
- Cleanup `cancelAnimationFrame` sur unmount.
- Fallback : si pas d'AnalyserNode après 30s, ref reste à 0 silencieusement.

Modifier `SphereCanvas` : ajouter prop `audioLevelRef?: React.RefObject<number>`. Dans la boucle de render interne (`loop()`), lire `audioLevelRef?.current ?? 0` et le passer au renderer dans `audio: ...` (à la place de l'actuel `audioRef.current` qui simule par sinusoïdes — remplacer la sim par le tap réel). Garder le `lerp` 0.25 pour smoother les bumps.

Modifier `SphereApp` pour wirer `const audioRef = useAudioLevel()` et passer à `<SphereCanvas audioLevelRef={audioRef} />`.

Tests :
- `useAudioLevel.test.ts` : mock `AudioContext` + `AnalyserNode` globaux dans le setup vitest. Test : fallback à 0 si `getAnalyser()` retourne `null`. Échantillonnage : feed un buffer connu (sinusoïde, max amplitude) → RMS calculé > 0.5.
- Test d'intégration léger : sphere reçoit un ref non-null, render() est appelé avec `audio > 0` quand le ref est forcé à 0.8.

## Acceptance criteria

- [ ] `audioPlayer.ts` insère un AnalyserNode dans le graphe Web Audio
- [ ] `getAnalyser()` exporté et utilisable
- [ ] `useAudioLevel` retourne un ref qui reflète le RMS de l'audio TTS sortant
- [ ] Pendant une réponse TTS, la sphère pulse visiblement en rythme avec la voix
- [ ] Pas de réponse TTS = sphere idle reste sereine (audioLevel = 0)
- [ ] Pas de fuite : `cancelAnimationFrame` sur unmount
- [ ] `?ui=legacy` (sans sphere) ne crash pas et n'installe pas l'analyser inutilement
- [ ] Tests Vitest passent
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0028-sphere-canvas-shader-port.md`
- `issues/0030-input-field-transcript-line.md`
