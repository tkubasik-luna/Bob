// sphere.jsx — React component wrapping the WebGL renderer.
// Owns: state crossfade, color interpolation, audio simulation, glyph overlay.

const { useEffect, useRef, useState, useMemo } = React;

const STATE_KEYS = ['idle', 'listen', 'think', 'speak', 'alert', 'error'];

const VARIANT_NAMES = [
  'liquid',   // 0
  'swarm',    // 1
  'wire',     // 2
  'plasma',   // 3
  'void',     // 4
  'glyph',    // 5
];

// Themes
const THEMES = {
  cold: {
    bg:      [0.008, 0.024, 0.055],   // #02060E
    accent:  [0.00,  0.90,  1.00],    // cyan
    accent2: [0.42,  0.71,  1.00],    // sky
  },
  warm: {
    bg:      [0.039, 0.024, 0.024],
    accent:  [1.00,  0.48,  0.27],    // warm orange
    accent2: [1.00,  0.71,  0.62],    // peach
  },
};

function hexToRgb01(hex) {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0,2),16)/255, parseInt(h.slice(2,4),16)/255, parseInt(h.slice(4,6),16)/255];
}

function lerp(a, b, t) { return a + (b - a) * t; }
function lerp3(a, b, t) { return [lerp(a[0],b[0],t), lerp(a[1],b[1],t), lerp(a[2],b[2],t)]; }

// Desaturate an RGB color: blend toward its luminance (mono),
// then nudge toward a hue tint for state-specific cast.
function desaturate(rgb, satKeep, tint, tintMix) {
  const lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2];
  const mono = [lum, lum, lum];
  let out = lerp3(rgb, mono, 1 - satKeep);
  if (tint && tintMix > 0) out = lerp3(out, tint, tintMix);
  return out;
}

// Calm mood state-specific tints (matches CSS --state-tint)
const CALM_STATE_TINTS = {
  idle:   [0.61, 0.66, 0.71],   // neutral gray
  listen: [0.56, 0.70, 0.78],   // sky
  think:  [0.66, 0.61, 0.75],   // lavender
  speak:  [0.56, 0.73, 0.62],   // mint
  alert:  [0.79, 0.65, 0.42],   // amber
  error:  [0.75, 0.55, 0.55],   // rose
};

// Glyph alphabet — mixed runes / math / katakana-ish marks for "alien" feel
const GLYPH_ALPHABET = '∆∇∮∯∰⊕⊗⊘⊙⊚⊛⊜⊝⌬⌭⌮¶§†‡¦◊◇◈ﾊﾐﾋｰｳｼﾅﾓﾆｻﾜﾂｵﾘｱﾎﾃﾏｹﾒｴｶｷﾑﾕﾗｾﾈｽﾀﾇﾍ0123456789אבגדהוCONSCIENCE';

function Sphere({ variant, state, motion, glow, theme, mood, audioSim }) {
  const canvasRef = useRef(null);
  const glyphRef = useRef(null);
  const rendererRef = useRef(null);
  const rafRef = useRef(0);

  // Animated state weights — crossfade between current/target
  const stateWeightsRef = useRef({ idle: 1, listen: 0, think: 0, speak: 0, alert: 0, error: 0 });
  const targetStateRef = useRef(state);
  const colorRef = useRef({
    bg: THEMES.cold.bg.slice(),
    accent: THEMES.cold.accent.slice(),
    accent2: THEMES.cold.accent2.slice(),
  });
  const motionRef = useRef(motion);
  const glowRef = useRef(glow);
  const variantRef = useRef(variant);
  const moodRef = useRef(mood);
  const audioRef = useRef(0);

  // Update targets when props change
  useEffect(() => { targetStateRef.current = state; }, [state]);
  useEffect(() => { variantRef.current = variant; }, [variant]);
  useEffect(() => { motionRef.current = motion; }, [motion]);
  useEffect(() => { glowRef.current = glow; }, [glow]);
  useEffect(() => { moodRef.current = mood; }, [mood]);

  // Init WebGL renderer
  useEffect(() => {
    const canvas = canvasRef.current;
    let renderer;
    try {
      renderer = window.SphereShader.createSphereRenderer(canvas);
      rendererRef.current = renderer;
    } catch (e) {
      console.error('Failed to create renderer', e);
      return;
    }

    const onResize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      renderer.setSize(canvas.clientWidth, canvas.clientHeight, dpr);
    };
    onResize();
    window.addEventListener('resize', onResize);

    const start = performance.now();
    let lastT = 0;

    function loop(now) {
      const dt = Math.min((now - lastT) / 1000, 0.05);
      lastT = now;
      const t = (now - start) / 1000;

      // Update audio sim — sum of slow + fast sines + noise blip during speak
      const target = targetStateRef.current;
      const speaking = target === 'speak' ? 1 : 0;
      const fakeAudio =
        speaking *
        Math.max(
          0,
          0.4 +
            0.3 * Math.sin(t * 5.2) +
            0.2 * Math.sin(t * 11.7 + 1.0) +
            0.15 * Math.sin(t * 23.1 + 2.4)
        );
      audioRef.current = lerp(audioRef.current, fakeAudio, 0.25);

      // Crossfade state weights
      const weights = stateWeightsRef.current;
      const rate = 1 - Math.exp(-dt * 4.5); // ~250ms to settle
      STATE_KEYS.forEach((k) => {
        const targetVal = (k === target) ? 1 : 0;
        weights[k] = lerp(weights[k], targetVal, rate);
      });

      // Color targets (alert/error override)
      const baseTheme = THEMES[theme] || THEMES.cold;
      let targetBg = baseTheme.bg;
      let targetAccent = baseTheme.accent;
      let targetAccent2 = baseTheme.accent2;
      if (target === 'alert') {
        targetAccent = [1.0, 0.7, 0.0];
        targetAccent2 = [1.0, 0.4, 0.0];
      } else if (target === 'error') {
        targetAccent = [1.0, 0.24, 0.24];
        targetAccent2 = [1.0, 0.55, 0.4];
      }
      // Mood: calm desaturates toward neutral gray + per-state hue tint
      if (moodRef.current === 'calm') {
        const tint = CALM_STATE_TINTS[target] || CALM_STATE_TINTS.idle;
        targetAccent = desaturate(targetAccent, 0.55, tint, 0.35);
        targetAccent2 = desaturate(targetAccent2, 0.55, tint, 0.30);
      }
      const cRate = 1 - Math.exp(-dt * 3.0);
      colorRef.current.bg = lerp3(colorRef.current.bg, targetBg, cRate);
      colorRef.current.accent = lerp3(colorRef.current.accent, targetAccent, cRate);
      colorRef.current.accent2 = lerp3(colorRef.current.accent2, targetAccent2, cRate);

      renderer.render({
        time: t,
        variant: variantRef.current,
        motion: motionRef.current,
        glow: glowRef.current,
        accent: colorRef.current.accent,
        accent2: colorRef.current.accent2,
        bg: colorRef.current.bg,
        audio: audioRef.current,
        states: weights,
      });

      // Glyph overlay (variant 5)
      drawGlyphOverlay(glyphRef.current, variantRef.current, weights, t, colorRef.current);

      rafRef.current = requestAnimationFrame(loop);
    }
    rafRef.current = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener('resize', onResize);
    };
  }, [theme]);

  return (
    <div className="sphere-stage">
      <canvas ref={canvasRef} className="sphere-canvas" />
      <canvas ref={glyphRef} className="glyph-overlay" />
    </div>
  );
}

// Glyph overlay — only visible for variant 5
function drawGlyphOverlay(canvas, variant, weights, t, colors) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const W = canvas.clientWidth * dpr;
  const H = canvas.clientHeight * dpr;
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;

  ctx.clearRect(0, 0, W, H);
  if (variant !== 5) return;

  ctx.save();
  ctx.translate(W / 2, H / 2);
  const size = Math.min(W, H);
  const R = size * 0.22;

  const accent = colors.accent.map((c) => Math.round(c * 255));
  const accent2 = colors.accent2.map((c) => Math.round(c * 255));

  // Generate ~140 glyph cells distributed on a fibonacci sphere
  const N = 180;
  const phi = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2;
    const rad = Math.sqrt(1 - y * y);
    const th = phi * i + t * 0.18;
    let x = Math.cos(th) * rad;
    let z = Math.sin(th) * rad;
    // Rotate around X axis slowly
    const cx = Math.cos(t * 0.07), sx = Math.sin(t * 0.07);
    const y2 = y * cx - z * sx;
    const z2 = y * sx + z * cx;
    const px = x * R;
    const py = y2 * R;
    const depth = (z2 + 1) / 2; // 0 back, 1 front
    if (z2 < -0.2) continue; // hide far back

    // Pick a glyph that morphs over time
    const seed = (i * 13 + Math.floor(t * 2 + i * 0.5)) % GLYPH_ALPHABET.length;
    const ch = GLYPH_ALPHABET[seed];

    const fontSize = (10 + depth * 12) * dpr;
    ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const alpha = 0.15 + depth * 0.65;
    // Some glyphs glow bright (the "active thoughts")
    const isHot = ((i + Math.floor(t * 1.2)) % 17) === 0;
    if (isHot) {
      ctx.fillStyle = `rgba(255,255,255,${alpha})`;
      ctx.shadowColor = `rgba(${accent[0]},${accent[1]},${accent[2]},1)`;
      ctx.shadowBlur = 14 * dpr;
    } else {
      ctx.fillStyle = `rgba(${accent[0]},${accent[1]},${accent[2]},${alpha * 0.85})`;
      ctx.shadowBlur = 0;
    }
    ctx.fillText(ch, px, py);
  }
  ctx.restore();
}

window.Sphere = Sphere;
window.VARIANT_NAMES = VARIANT_NAMES;
window.STATE_KEYS = STATE_KEYS;
