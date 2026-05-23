// sphere-shader.js
// WebGL2 fragment shader for the central orb. Single full-screen quad.
// 6 variants × 6 states, modulated via uniforms.
//
// State params are independent (uBreath, uListenWave, uThinkChaos, uSpeakPulse,
// uAlertMix, uErrorGlitch) so transitions crossfade naturally — when state
// changes, JS lerps the target on / others off.

window.SphereShader = (function () {
  const VERT = `#version 300 es
  in vec2 aPos;
  void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }`;

  const FRAG = `#version 300 es
  precision highp float;
  out vec4 fragColor;

  uniform vec2  uRes;
  uniform float uTime;
  uniform int   uVariant;
  uniform float uMotion;       // 0..1 ambient motion intensity
  uniform float uGlow;         // 0..1 glow intensity
  uniform vec3  uAccent;
  uniform vec3  uAccent2;
  uniform vec3  uBg;
  uniform float uAudio;        // simulated audio level 0..1

  // State weights (each in 0..1; sum is roughly 1 after crossfade)
  uniform float uIdle;
  uniform float uListen;
  uniform float uThink;
  uniform float uSpeak;
  uniform float uAlert;
  uniform float uError;

  // ------- hash / noise -------
  float hash11(float p){ p = fract(p*.1031); p *= p+33.33; p *= p+p; return fract(p); }
  float hash21(vec2 p){ vec3 p3=fract(vec3(p.xyx)*.1031); p3+=dot(p3,p3.yzx+33.33); return fract((p3.x+p3.y)*p3.z); }
  vec3  hash33(vec3 p){ p = vec3(dot(p,vec3(127.1,311.7,74.7)), dot(p,vec3(269.5,183.3,246.1)), dot(p,vec3(113.5,271.9,124.6))); return -1.0+2.0*fract(sin(p)*43758.5453123); }
  float hash31(vec3 p){ p=fract(p*.1031); p+=dot(p,p.yzx+33.33); return fract((p.x+p.y)*p.z); }

  float vnoise(vec3 x){
    vec3 i=floor(x), f=fract(x); f=f*f*(3.0-2.0*f);
    return mix(mix(mix(hash31(i+vec3(0,0,0)),hash31(i+vec3(1,0,0)),f.x),
                   mix(hash31(i+vec3(0,1,0)),hash31(i+vec3(1,1,0)),f.x),f.y),
               mix(mix(hash31(i+vec3(0,0,1)),hash31(i+vec3(1,0,1)),f.x),
                   mix(hash31(i+vec3(0,1,1)),hash31(i+vec3(1,1,1)),f.x),f.y),f.z);
  }
  float fbm(vec3 p){ float v=0., a=.5; for(int i=0;i<5;i++){ v+=a*vnoise(p); p*=2.03; a*=.5; } return v; }
  mat2 rot(float a){ float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }

  // map screen uv to a fake-3d unit sphere surface; returns vec4(normal.xyz, alpha)
  // alpha = 0 outside, smooth edge inside
  vec4 sphereSample(vec2 uv, float R){
    float r2 = dot(uv,uv);
    float R2 = R*R;
    if(r2 > R2*1.6) return vec4(0.0);
    float z = sqrt(max(R2 - r2, 0.0)) / R;
    vec3 n = vec3(uv/R, z);
    float a = smoothstep(R2*1.05, R2*0.94, r2);
    return vec4(n, a);
  }

  // luminance helper
  float lum(vec3 c){ return dot(c, vec3(.299,.587,.114)); }

  // ===== VARIANT 0 — LIQUID MERCURY =====
  vec3 variantLiquid(vec3 n, float alpha, vec2 uv, float t){
    // Displace surface using fbm to create flowing chrome blobs
    vec3 p = n * 1.7 + vec3(0.0, 0.0, t*0.15);
    float disp = fbm(p);
    float disp2 = fbm(p*2.3 + 11.0);
    // Fake reflective shading using y-coord (env top vs bottom)
    float lat = n.y;
    float ang = atan(n.y, n.x);
    // Chrome bands
    float bands = 0.5 + 0.5 * sin(disp*8.0 + lat*4.0 + t*.2);
    bands = pow(bands, 2.0);
    // Iridescent shift
    vec3 chrome = mix(uBg*1.5 + uAccent*.05, uAccent, bands);
    chrome = mix(chrome, uAccent2, pow(disp2, 2.0)*.7);
    // Rim
    float rim = pow(1.0 - n.z, 2.0);
    chrome += uAccent * rim * 0.9;
    // Specular highlight
    vec3 L = normalize(vec3(0.4, 0.6, 0.7));
    float spec = pow(max(dot(n, L), 0.0), 30.0);
    chrome += vec3(spec) * 1.2;
    // Surface flow lines
    float flow = smoothstep(0.5, 0.55, fract(disp*5.0 + t*0.1));
    chrome += uAccent2 * flow * 0.15;
    return chrome * alpha;
  }

  // ===== VARIANT 1 — SWARM (volumetric particles) =====
  vec3 variantSwarm(vec3 n, float alpha, vec2 uv, float t, float R){
    // Raymarch through a spherical shell sampling sparse particle density
    vec3 col = vec3(0.0);
    float r2 = dot(uv,uv);
    if(r2 > R*R*1.4) return col;
    // Multiple shells front-to-back
    for(int i=0; i<6; i++){
      float fi = float(i)/5.0;
      // Sample radius
      float sr = R * (0.55 + fi*0.55);
      float r = length(uv);
      if(r > sr) continue;
      float z = sqrt(max(sr*sr - r*r, 0.0));
      // Pick front or back hemisphere randomly per shell
      float zSign = (mod(float(i),2.0) < 1.0) ? 1.0 : -1.0;
      vec3 p = vec3(uv, z*zSign);
      // Rotate over time
      float rotSpeed = 0.15 + fi*0.1;
      p.xz = rot(t * rotSpeed) * p.xz;
      p.yz = rot(t * rotSpeed * 0.7 + fi) * p.yz;
      // Voronoi-ish particle sampling
      vec3 g = floor(p*22.0);
      vec3 f = fract(p*22.0) - 0.5;
      float minD = 1.0;
      for(int dx=-1; dx<=1; dx++){
        for(int dy=-1; dy<=1; dy++){
          vec3 o = vec3(float(dx), float(dy), 0.0);
          vec3 c = hash33(g+o);
          c.z = 0.0;
          vec3 d = o + c*0.5 - f;
          d.z = 0.0;
          minD = min(minD, dot(d,d));
        }
      }
      float spark = exp(-minD*120.0);
      float depth = (z * zSign / R) * 0.5 + 0.5;
      vec3 c = mix(uAccent2*0.6, uAccent, depth);
      col += spark * c * (0.9 - fi*0.1);
    }
    // Soft inner glow
    float r = length(uv);
    col += uAccent * exp(-r*r*40.0) * 0.18;
    return col;
  }

  // ===== VARIANT 2 — WIRE / LATTICE =====
  vec3 variantWire(vec3 n, float alpha, vec2 uv, float t){
    if(alpha < 0.001) return vec3(0.0);
    // Rotate normal
    vec3 p = n;
    p.xz = rot(t*0.15) * p.xz;
    p.yz = rot(t*0.07 + 1.3) * p.yz;
    // Lat/long lines
    float theta = atan(p.x, p.z);
    float phi   = asin(clamp(p.y, -1.0, 1.0));
    float lng = abs(fract(theta * 6.0 / 6.2831 + 0.5) - 0.5);
    float lat = abs(fract(phi * 6.0 / 3.1416 + 0.5) - 0.5);
    float w = smoothstep(0.02, 0.0, min(lng, lat));
    // Icosahedral-ish vertex highlights via cellular noise on sphere
    vec3 q = p * 2.6;
    vec3 g = floor(q);
    vec3 f = fract(q) - 0.5;
    float minD = 1.0;
    for(int dx=-1; dx<=1; dx++) for(int dy=-1; dy<=1; dy++) for(int dz=-1; dz<=1; dz++){
      vec3 o = vec3(float(dx),float(dy),float(dz));
      vec3 c = hash33(g+o)*0.5;
      float d = length(o + c - f);
      minD = min(minD, d);
    }
    float verts = exp(-minD*minD*60.0);
    // Compose
    vec3 col = uAccent * w * 0.9;
    col += uAccent2 * verts * 1.4;
    // Faint inner haze
    col += uAccent * (0.06 + 0.06 * fbm(p*3.0 + t*0.1));
    // Rim halo
    float rim = pow(1.0 - n.z, 4.0);
    col += uAccent * rim * 0.8;
    return col * alpha;
  }

  // ===== VARIANT 3 — PLASMA CORE =====
  vec3 variantPlasma(vec3 n, float alpha, vec2 uv, float t, float R){
    // Raymarch a soft volumetric inside the sphere
    vec3 col = vec3(0.0);
    float r = length(uv);
    if(r > R*1.3) return col;
    // Simple 6-step march along z through sphere
    float total = 0.0;
    for(int i=0; i<10; i++){
      float fi = float(i)/9.0;
      float z = mix(-R, R, fi);
      float rho = sqrt(max(R*R - uv.x*uv.x - uv.y*uv.y - 0.0, 0.0));
      if(abs(z) > rho) continue;
      vec3 p = vec3(uv, z);
      p.xy = rot(t*0.2) * p.xy;
      p.xz = rot(t*0.15 + 0.5) * p.xz;
      float d = fbm(p*3.2 + t*0.4);
      d = pow(d, 2.0);
      // Heat falloff from center
      float radial = 1.0 - length(p)/R;
      float h = max(radial, 0.0) * d;
      total += h;
    }
    total /= 10.0;
    // Map heat → color (deep core → outer flames)
    vec3 c = mix(uBg, uAccent2*0.8, smoothstep(0.0, 0.3, total));
    c = mix(c, uAccent, smoothstep(0.15, 0.55, total));
    c = mix(c, vec3(1.0), smoothstep(0.45, 0.85, total));
    // Edge softness
    float edge = smoothstep(R*1.15, R*0.85, r);
    col = c * edge * 1.4;
    // Rim glow
    float rim = exp(-pow((r-R*0.96)/(R*0.08), 2.0));
    col += uAccent * rim * 0.6;
    return col;
  }

  // ===== VARIANT 4 — VOID / PORTAL =====
  vec3 variantVoid(vec3 n, float alpha, vec2 uv, float t, float R){
    // Distort background grid around the sphere — gravitational lens
    float r = length(uv);
    // Lens displacement
    vec2 dir = (r > 0.0001) ? uv/r : vec2(1.0,0.0);
    float lens = 1.0 / (1.0 + (r/R)*(r/R)*3.5);
    vec2 luv = uv - dir * lens * R * 0.25;
    // Background lattice
    vec2 g = luv * 14.0;
    g.x += t*0.1; g.y -= t*0.05;
    vec2 gf = abs(fract(g) - 0.5);
    float grid = smoothstep(0.49, 0.5, max(gf.x, gf.y));
    // Concentric breath rings
    float rings = 0.5 + 0.5 * sin(length(luv)*36.0 - t*1.4);
    rings = pow(rings, 14.0);
    vec3 bg = uBg + uAccent * (grid*0.35 + rings*0.4);
    // Inside sphere = pure black with rim only
    float inside = smoothstep(R*0.96, R*0.92, r);
    bg *= 1.0 - inside;
    // Rim flame
    float rim = exp(-pow((r-R*0.98)/(R*0.04), 2.0));
    bg += uAccent * rim * 1.2;
    // Inner star — single bright point at center
    float core = exp(-r*r*900.0);
    bg += vec3(1.0) * core * 0.8;
    return bg;
  }

  // ===== VARIANT 5 — GLYPH SHELL (shader bg; canvas2D draws glyphs above) =====
  vec3 variantGlyph(vec3 n, float alpha, vec2 uv, float t, float R){
    // Soft shell that hints at structure; glyphs are overlaid by canvas2D
    if(alpha < 0.001) return vec3(0.0);
    vec3 p = n;
    p.xz = rot(t*0.1) * p.xz;
    float band = sin(p.y * 12.0 + t*0.3) * 0.5 + 0.5;
    band = pow(band, 4.0);
    float n1 = fbm(p*4.0 + t*0.15);
    vec3 col = uAccent * (0.06 + 0.18*n1) * alpha;
    col += uAccent2 * band * 0.15 * alpha;
    // Rim
    float rim = pow(1.0 - n.z, 3.0);
    col += uAccent * rim * 0.5;
    return col;
  }

  // ===== STATE OVERLAYS =====
  // Listen — waveform ring radiating inward toward sphere
  vec3 listenOverlay(vec2 uv, float t, float R){
    float r = length(uv);
    if(r < R*0.5 || r > R*2.0) return vec3(0.0);
    float ang = atan(uv.y, uv.x);
    // Multiple traveling waves
    float w = 0.0;
    for(int i=0; i<5; i++){
      float fi = float(i);
      float speed = 0.8 + fi*0.2;
      float freq = 16.0 + fi*4.0;
      float amp = 0.02 / (1.0 + fi);
      float wr = R * (1.15 + fi*0.12) + sin(ang*freq + t*speed*3.0) * amp + sin(t*speed)*amp;
      w += exp(-pow((r - wr)/0.008, 2.0)) * (1.0 - fi*0.15);
    }
    return uAccent * w * 0.9;
  }

  // Think — turbulent particles swirling around
  vec3 thinkOverlay(vec2 uv, float t, float R){
    float r = length(uv);
    if(r > R*2.2) return vec3(0.0);
    // Swirling field
    float ang = atan(uv.y, uv.x);
    vec2 swirl = uv;
    swirl = rot(t*0.4 + r*3.0) * swirl;
    float n1 = fbm(vec3(swirl*7.0, t*0.6));
    n1 = pow(n1, 3.0);
    float ringMask = smoothstep(R*0.7, R*1.1, r) * smoothstep(R*2.0, R*1.3, r);
    vec3 col = mix(uAccent2, uAccent, n1) * n1 * ringMask * 0.7;
    // Sparks
    vec2 sp = uv*8.0;
    sp = rot(t*0.5) * sp;
    vec2 sg = floor(sp); vec2 sf = fract(sp) - 0.5;
    float sparkD = 1.0;
    for(int dx=-1; dx<=1; dx++) for(int dy=-1; dy<=1; dy++){
      vec2 o = vec2(float(dx),float(dy));
      vec3 c = hash33(vec3(sg+o, floor(t*2.0)));
      float d = length(o + c.xy*0.5 - sf);
      sparkD = min(sparkD, d);
    }
    float sparks = exp(-sparkD*sparkD*200.0) * ringMask;
    col += uAccent * sparks * 1.5;
    return col;
  }

  // Speak — radial pulse waves outward, audio-reactive
  vec3 speakOverlay(vec2 uv, float t, float R, float audio){
    float r = length(uv);
    if(r < R*0.9 || r > R*2.4) return vec3(0.0);
    float w = 0.0;
    for(int i=0; i<4; i++){
      float fi = float(i);
      float phase = mod(t*0.6 + fi*0.4, 1.0);
      float wr = R * (1.0 + phase*1.4);
      float amp = (1.0 - phase) * (0.4 + audio*0.6);
      w += exp(-pow((r - wr)/(0.02 + phase*0.04), 2.0)) * amp;
    }
    return uAccent * w * 1.1;
  }

  // Idle — gentle breath halo
  vec3 idleHalo(vec2 uv, float t, float R){
    float r = length(uv);
    float breath = 0.5 + 0.5*sin(t*0.9);
    float halo = exp(-pow((r-R*1.05)/(0.08 + breath*0.05), 2.0));
    return uAccent * halo * 0.25;
  }

  // Alert tint shift — modulates whole color toward amber/red
  vec3 alertTint(vec3 col, float t, float strength){
    if(strength < 0.001) return col;
    float pulse = 0.7 + 0.3*sin(t*8.0);
    vec3 amber = vec3(1.0, 0.65, 0.1);
    return mix(col, col*amber*pulse*1.2, strength);
  }

  // Error glitch — chromatic split + noise lines
  vec2 errorOffset(vec2 uv, float t, float strength){
    if(strength < 0.001) return uv;
    float band = step(0.85, hash21(vec2(floor(uv.y*30.0), floor(t*40.0))));
    float jx = (hash21(vec2(floor(t*20.0), floor(uv.y*20.0))) - 0.5) * 0.06 * strength * band;
    return uv + vec2(jx, 0.0);
  }

  void main(){
    vec2 frag = gl_FragCoord.xy;
    vec2 uv = (frag - uRes*0.5) / min(uRes.x, uRes.y);
    float t = uTime * (0.4 + uMotion*0.9);
    float R = 0.22;

    // Apply error glitch displacement to uv
    uv = errorOffset(uv, uTime, uError);

    vec4 ns = sphereSample(uv, R);
    vec3 n = ns.xyz;
    float a = ns.w;

    vec3 col = vec3(0.0);
    if(uVariant == 0) col = variantLiquid(n, a, uv, t);
    else if(uVariant == 1) col = variantSwarm(n, a, uv, t, R);
    else if(uVariant == 2) col = variantWire(n, a, uv, t);
    else if(uVariant == 3) col = variantPlasma(n, a, uv, t, R);
    else if(uVariant == 4) col = variantVoid(n, a, uv, t, R);
    else col = variantGlyph(n, a, uv, t, R);

    // STATE OVERLAYS
    col += idleHalo(uv, t, R) * uIdle;
    col += listenOverlay(uv, t, R) * uListen;
    col += thinkOverlay(uv, t, R) * uThink;
    col += speakOverlay(uv, t, R, uAudio) * uSpeak;

    // Pulse breathing scale on idle (subtle radial brightening)
    float r = length(uv);
    float breath = 0.5 + 0.5*sin(t*0.9);
    col *= 1.0 + uIdle * (breath*0.08);

    // Alert color shift
    col = alertTint(col, uTime, uAlert);

    // Error: add scanline noise
    if(uError > 0.001){
      float scan = step(0.5, sin(uv.y*200.0 + uTime*30.0));
      col += uAccent * scan * uError * 0.08;
      // RGB split visible: cheap version — boost R channel
      col.r *= 1.0 + uError*0.4;
      col.b *= 1.0 + uError*0.2;
    }

    // Global glow boost (cheap bloom approximation — sample neighbors not done here,
    // but boost bright pixels and let the JS-side feedback fake it)
    col *= 0.85 + uGlow * 0.7;

    // Subtle vignette
    float vign = smoothstep(1.25, 0.25, length(uv*vec2(uRes.x/uRes.y, 1.0)));
    col *= 0.92 + vign*0.18;

    // Background
    vec3 finalC = uBg + col;

    // Subtle scan lines (very faint — always on)
    float sl = 0.5 + 0.5*sin(frag.y*1.3);
    finalC *= 0.985 + sl*0.015;

    // Film grain
    float grain = (hash21(frag + floor(uTime*60.0)) - 0.5) * 0.02;
    finalC += grain;

    fragColor = vec4(finalC, 1.0);
  }`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.error('Shader compile error:', gl.getShaderInfoLog(s));
      console.error(src);
      throw new Error('Shader compile failed');
    }
    return s;
  }

  function createSphereRenderer(canvas) {
    const gl = canvas.getContext('webgl2', { antialias: true, premultipliedAlpha: false });
    if (!gl) throw new Error('WebGL2 not supported');

    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error('Program link: ' + gl.getProgramInfoLog(prog));
    }
    gl.useProgram(prog);

    // Full-screen quad
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      -1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1
    ]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, 'aPos');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

    const U = {};
    const names = ['uRes', 'uTime', 'uVariant', 'uMotion', 'uGlow', 'uAccent', 'uAccent2', 'uBg', 'uAudio', 'uIdle', 'uListen', 'uThink', 'uSpeak', 'uAlert', 'uError'];
    names.forEach(n => U[n] = gl.getUniformLocation(prog, n));

    function setSize(w, h, dpr) {
      const W = Math.floor(w * dpr);
      const H = Math.floor(h * dpr);
      if (canvas.width !== W) canvas.width = W;
      if (canvas.height !== H) canvas.height = H;
      gl.viewport(0, 0, W, H);
      gl.uniform2f(U.uRes, W, H);
    }

    function render(uniforms) {
      gl.uniform1f(U.uTime, uniforms.time);
      gl.uniform1i(U.uVariant, uniforms.variant);
      gl.uniform1f(U.uMotion, uniforms.motion);
      gl.uniform1f(U.uGlow, uniforms.glow);
      gl.uniform3fv(U.uAccent, uniforms.accent);
      gl.uniform3fv(U.uAccent2, uniforms.accent2);
      gl.uniform3fv(U.uBg, uniforms.bg);
      gl.uniform1f(U.uAudio, uniforms.audio);
      gl.uniform1f(U.uIdle, uniforms.states.idle);
      gl.uniform1f(U.uListen, uniforms.states.listen);
      gl.uniform1f(U.uThink, uniforms.states.think);
      gl.uniform1f(U.uSpeak, uniforms.states.speak);
      gl.uniform1f(U.uAlert, uniforms.states.alert);
      gl.uniform1f(U.uError, uniforms.states.error);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    return { gl, setSize, render };
  }

  return { createSphereRenderer };
})();
