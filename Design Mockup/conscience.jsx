// conscience.jsx — React wrapper around the WebGL renderer + life engine.
// Owns the rAF loop, feeds cursor position to the life engine, and reports
// live vitals upward each frame (throttled) so the HUD can display them.

const { useEffect: useEffectC, useRef: useRefC } = React;

function Conscience({ form, state, motion, glow, breathDepth, gazeGain, neb, palettes, tint, onVitals }) {
  const canvasRef = useRefC(null);
  const rendererRef = useRefC(null);
  const lifeRef = useRefC(null);
  const rafRef = useRefC(0);

  const formRef = useRefC(form);
  const motionRef = useRefC(motion);
  const glowRef = useRefC(glow);
  const breathRef = useRefC(breathDepth);
  const gazeRef = useRefC(gazeGain);
  const nebRef = useRefC(neb);
  const nebCurRef = useRefC(neb ? { ...neb } : null);   // eased toward the target preset
  const orbitPhaseRef = useRefC(0);                     // accumulated orbital phase
  const tintRef = useRefC(tint);
  const onVitalsRef = useRefC(onVitals);

  useEffectC(() => { formRef.current = form; }, [form]);
  useEffectC(() => { motionRef.current = motion; }, [motion]);
  useEffectC(() => { glowRef.current = glow; }, [glow]);
  useEffectC(() => { breathRef.current = breathDepth; }, [breathDepth]);
  useEffectC(() => { gazeRef.current = gazeGain; }, [gazeGain]);
  useEffectC(() => { nebRef.current = neb; }, [neb]);
  useEffectC(() => { tintRef.current = tint; }, [tint]);
  useEffectC(() => { onVitalsRef.current = onVitals; }, [onVitals]);
  useEffectC(() => { if (lifeRef.current) lifeRef.current.setState(state); }, [state]);

  useEffectC(() => {
    const canvas = canvasRef.current;
    let renderer;
    try {
      renderer = window.ConscienceShader.createRenderer(canvas);
      rendererRef.current = renderer;
    } catch (e) {
      console.error('renderer init failed', e);
      return;
    }
    const life = new window.ConscienceLife.LifeEngine(palettes ? { palettes } : {});
    life.setState(state);
    lifeRef.current = life;

    let mousePx = null;
    const size = { w: canvas.clientWidth, h: canvas.clientHeight };
    const onResize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      size.w = canvas.clientWidth; size.h = canvas.clientHeight;
      renderer.setSize(size.w, size.h, dpr);
    };
    onResize();
    window.addEventListener('resize', onResize);

    const onMove = (e) => { mousePx = [e.clientX, e.clientY]; };
    const onLeave = () => { mousePx = null; };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerleave', onLeave);

    let lastT = performance.now();
    let vitalsAccum = 0;

    function loop(now) {
      const dt = (now - lastT) / 1000;
      lastT = now;

      life.setMouse(mousePx, size);
      life.update(dt, { motion: motionRef.current, breathDepth: breathRef.current, gazeGain: gazeRef.current });
      const u = life.uniforms(formRef.current, motionRef.current, glowRef.current);
      // Ease every neb parameter toward the active state's preset so transitions
      // between idle / listen / think / … read as one organic body settling.
      const target = nebRef.current;
      if (target) {
        if (!nebCurRef.current) nebCurRef.current = { ...target };
        const cur = nebCurRef.current;
        const k = 1 - Math.exp(-dt / 0.55);   // ~0.55s time-constant
        for (const key in target) {
          const tv = target[key];
          if (typeof tv !== 'number') { cur[key] = tv; continue; }
          cur[key] = cur[key] + (tv - cur[key]) * k;
        }
        u.neb = cur;
        // integrate orbital phase from the (eased) speed so the satellites turn
        // at a steady rate and never reverse, even while the speed eases.
        orbitPhaseRef.current += (cur.trailSpeed || 0) * dt;
        cur.orbitPhase = orbitPhaseRef.current;
      }
      if (tintRef.current) u.tint = tintRef.current;
      renderer.render(u);

      vitalsAccum += dt;
      if (vitalsAccum > 0.1 && onVitalsRef.current) {  // ~10Hz HUD update
        vitalsAccum = 0;
        onVitalsRef.current({
          breath: life.vitals.breath,
          attention: life.vitals.attention,
          gaze: [life.vitals.gaze[0], life.vitals.gaze[1]],
          bpm: life.vitals.bpm,
          weights: { ...life.weights },
        });
      }
      rafRef.current = requestAnimationFrame(loop);
    }
    rafRef.current = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerleave', onLeave);
    };
  }, []);

  return <canvas ref={canvasRef} className="cv-canvas" />;
}

window.Conscience = Conscience;
