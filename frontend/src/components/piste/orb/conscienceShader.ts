// conscienceShader.ts
// WebGL2 fragment shader for Bob's living consciousness orb.
//
// Ported VERBATIM from `Design Mockup/conscience-shader.js` (DO NOT modify the
// mockup). The mockup ships four organic forms (Souffle / Iris / Murmure /
// Nébuleuse) sharing one membrane; the Piste 3D · Nacre core uses FORM 3
// (NEBULEUSE) — a true ray-traced 3D glass orb with precessing comet trails,
// re-sampled through refraction — driven by per-state weights so moods melt
// into each other. The full GLSL is kept as-is (form selection is a uniform);
// only the JS renderer factory is re-typed for TS and made to return `null`
// on a missing WebGL2 context (so the React wrapper can render an HTML error
// banner, matching `sphere/sphereShader.ts`'s contract) instead of throwing.
//
// Aliveness is driven from JS via uniforms the life engine integrates
// (uBreath / uGaze / uAttention / uBlink / uDrift / uWobble) plus per-state
// weights (uIdle…uError) and the Nébuleuse tweak controls (uTrail*, uFogAmt,
// uIorT, uRimT, …) and a glass `uTint`.
//
// PRD: prd/0014-hud-piste-3d-nacre.md — Issue: issues/0084-conscience-orb-orbstate-reducer.md

export type StateWeights = {
  idle: number;
  listen: number;
  think: number;
  speak: number;
  alert: number;
  error: number;
};

export type Rgb = readonly [number, number, number];

/** Nébuleuse (form 3) tweak controls — eased per-frame toward the active
 * state's preset (`NEB_PRESETS` in the wrapper). `orbitPhase` is integrated
 * monotonically from `trailSpeed` so the satellites never reverse. */
export type NebParams = {
  trailCount: number;
  trailSpeed: number;
  trailLen: number;
  trailWidth: number;
  trailAlt: number;
  trailGlow: number;
  sphereSize: number;
  coreGlow: number;
  fogAmt: number;
  ior: number;
  rim: number;
  equator: number;
  latitude: number;
  orbitPhase: number;
};

export type ConscienceRenderParams = {
  time: number;
  form: number;
  motion: number;
  glow: number;
  accent: Rgb;
  accent2: Rgb;
  accent3: Rgb;
  bg: Rgb;
  audio: number;
  breath: number;
  gaze: readonly [number, number];
  attention: number;
  blink: number;
  drift: readonly [number, number];
  wobble: number;
  states: StateWeights;
  neb: NebParams;
  tint: Rgb;
};

export type ConscienceRenderer = {
  setSize(width: number, height: number, dpr: number): void;
  render(params: ConscienceRenderParams): void;
};

const VERT = `#version 300 es
in vec2 aPos;
void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }`;

// GLSL ported verbatim from Design Mockup/conscience-shader.js.
const FRAG = `#version 300 es
precision highp float;
out vec4 fragColor;

uniform vec2  uRes;
uniform float uTime;
uniform int   uForm;
uniform float uMotion;
uniform float uGlow;
uniform vec3  uAccent;
uniform vec3  uAccent2;
uniform vec3  uAccent3;
uniform vec3  uBg;
uniform float uAudio;

uniform float uBreath;
uniform vec2  uGaze;
uniform float uAttention;
uniform float uBlink;
uniform vec2  uDrift;
uniform float uWobble;

uniform float uIdle;
uniform float uListen;
uniform float uThink;
uniform float uSpeak;
uniform float uAlert;
uniform float uError;

// ---- Nebuleuse tweak controls ----
uniform float uTrailCount, uTrailSpeed, uTrailLen, uTrailWidth, uTrailAlt, uTrailGlow;
uniform float uSphereSize, uCoreGlowT, uFogAmt, uIorT, uRimT, uEquator, uLatitude;
uniform float uOrbitPhase;   // accumulated orbital phase (monotonic → never reverses)
uniform vec3  uTint;         // glass tint (warm for Bob, cool/rose for other screens)

// ---------- hash / noise ----------
float hash11(float p){ p=fract(p*.1031); p*=p+33.33; p*=p+p; return fract(p); }
float hash21(vec2 p){ vec3 p3=fract(vec3(p.xyx)*.1031); p3+=dot(p3,p3.yzx+33.33); return fract((p3.x+p3.y)*p3.z); }
vec3  hash33(vec3 p){ p=vec3(dot(p,vec3(127.1,311.7,74.7)),dot(p,vec3(269.5,183.3,246.1)),dot(p,vec3(113.5,271.9,124.6))); return -1.0+2.0*fract(sin(p)*43758.5453123); }
float hash31(vec3 p){ p=fract(p*.1031); p+=dot(p,p.yzx+33.33); return fract((p.x+p.y)*p.z); }
float vnoise(vec3 x){
  vec3 i=floor(x), f=fract(x); f=f*f*(3.0-2.0*f);
  return mix(mix(mix(hash31(i+vec3(0,0,0)),hash31(i+vec3(1,0,0)),f.x),
                 mix(hash31(i+vec3(0,1,0)),hash31(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash31(i+vec3(0,0,1)),hash31(i+vec3(1,0,1)),f.x),
                 mix(hash31(i+vec3(0,1,1)),hash31(i+vec3(1,1,1)),f.x),f.y),f.z);
}
float fbm(vec3 p){ float v=0.,a=.5; for(int i=0;i<5;i++){ v+=a*vnoise(p); p*=2.03; a*=.5; } return v; }
mat2 rot(float a){ float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }
float lum(vec3 c){ return dot(c, vec3(.299,.587,.114)); }

// map screen uv to fake-3d unit sphere; returns (normal.xyz, alpha)
vec4 sphereSample(vec2 uv, float R){
  float r2=dot(uv,uv); float R2=R*R;
  if(r2 > R2*1.7) return vec4(0.0);
  float z=sqrt(max(R2-r2,0.0))/R;
  vec3 n=vec3(uv/R, z);
  float a=smoothstep(R2*1.06, R2*0.93, r2);
  return vec4(n,a);
}

// ============ FORM 0 — SOUFFLE (breathing nucleus) ============
vec3 formSouffle(vec2 uv, float t, float R){
  float r=length(uv);
  float Rb = R*(1.0 + uBreath*0.06);
  // asymmetric wobble — never a perfect sphere
  float ang=atan(uv.y,uv.x);
  float wob = fbm(vec3(cos(ang)*1.4, sin(ang)*1.4, t*0.22))-0.5;
  float Rd = Rb*(1.0 + wob*(0.06+uWobble*0.10));
  vec4 ns=sphereSample(uv,Rd);
  vec3 n=ns.xyz; float a=ns.w;
  if(a<0.001){
    float halo=exp(-pow((r-Rd*1.05)/(0.05+uBreath*0.05),2.0));
    return uAccent*halo*0.30*(0.5+uBreath*0.5);
  }
  float fres=pow(1.0-n.z,2.5);
  // internal molten fluid — turbulence climbs with think + speak
  float turb = 0.10 + uThink*0.55 + uSpeak*0.25;
  vec3 p=n*1.6 + vec3(uGaze*0.5, t*(0.10+turb));
  float flow=fbm(p*1.8);
  float flow2=fbm(p*3.6 + 5.0);
  float core=pow(smoothstep(Rd,0.0,r),1.5);
  vec3 col=mix(uBg, uAccent2, core*0.6);
  col=mix(col, uAccent, smoothstep(0.2,0.85,flow)*(0.4+core*0.6));
  col+=uAccent2*pow(flow2,2.0)*(0.35+turb*0.4);
  col*=0.78 + uBreath*0.5;
  // gaze lobe — the glow leans toward where it is looking
  vec2 gdir=uGaze;
  float gl=dot(normalize(n.xy+1e-4), normalize(gdir+1e-4));
  float lobe=max(0.0,n.z)*smoothstep(0.1,1.0,gl);
  col+=uAccent3*lobe*(0.18+uAttention*0.6)*(0.4+core);
  // warm fresnel rim, breathing
  col+=uAccent*fres*(0.5+uBreath*0.45);
  // specular catch toward gaze
  vec3 L=normalize(vec3(gdir*0.6, 0.85));
  float spec=pow(max(dot(n,L),0.0),42.0);
  col+=vec3(1.0,0.96,0.9)*spec*(0.25+uAttention*0.5);
  return col*a;
}

// ============ FORM 1 — IRIS (the eye that watches) ============
vec3 formIris(vec2 uv, float t, float R){
  float r=length(uv);
  float Rb=R*(1.0+uBreath*0.035);
  vec4 ns=sphereSample(uv,Rb);
  vec3 n=ns.xyz; float a=ns.w;
  if(a<0.001){
    float halo=exp(-pow((r-Rb*1.04)/0.05,2.0));
    return uAccent*halo*0.22;
  }
  // iris slides toward gaze across the curved eyeball
  vec2 ic=uGaze*Rb*0.34;
  vec2 d=uv-ic;
  float ir=length(d);
  float iang=atan(d.y,d.x);
  float irisR=Rb*0.60;
  // pupil dilates when unfocused / breathing, constricts under attention & alert
  float pupilR=Rb*(0.13 + 0.15*(1.0-uAttention) + 0.05*uBreath + uThink*0.05 - uAlert*0.04);
  pupilR=max(pupilR, Rb*0.05);
  // radial fibres
  float fibSeed=fbm(vec3(cos(iang)*3.0, sin(iang)*3.0, ir*7.0 + t*0.05));
  float fibers=pow(0.5+0.5*sin(iang*58.0 + fibSeed*7.0), 1.4);
  float irT=smoothstep(pupilR,irisR,ir);
  vec3 iris=mix(uAccent2*1.15, uAccent*0.7, irT);
  iris=mix(iris, uBg*1.5, smoothstep(irisR*0.78,irisR,ir));   // limbal ring
  iris*=0.45+fibers*0.75;
  iris+=uAccent3*pow(fbm(vec3(d*30.0, t*0.12)),3.0)*0.5;       // crypts/flecks
  // pupil
  float pup=smoothstep(pupilR, pupilR*0.84, ir);
  iris=mix(iris, vec3(0.02,0.012,0.01), pup);
  iris+=uAccent*smoothstep(pupilR*1.18,pupilR,ir)*smoothstep(pupilR*0.88,pupilR,ir)*0.45;
  // sclera (warm subsurface) outside iris
  float outIris=smoothstep(irisR,irisR*1.05,ir);
  vec3 sclera=mix(uBg*2.0, uAccent*0.30, pow(1.0-n.z,2.0));
  vec3 col=mix(iris, sclera, outIris);
  // catchlight upper-left, drifts opposite gaze (wet highlight)
  vec2 cl=ic+vec2(-0.05,0.05)*Rb - uGaze*0.02;
  float catchL=exp(-pow(length(uv-cl)/(Rb*0.06),2.0));
  col+=vec3(1.0,0.97,0.92)*catchL*0.85;
  col+=uAccent3*pow(1.0-n.z,3.0)*0.45;                        // wet rim
  // eyelid blink — lids close from top & bottom toward centre
  float ny=uv.y/Rb;
  float cover=smoothstep(1.0-uBlink-0.05, 1.0-uBlink, abs(ny));
  vec3 lidCol=mix(uBg*1.6, uAccent*0.25, 0.4);
  col=mix(col, lidCol, cover);
  return col*a;
}

// ============ FORM 2 — MURMURE (living swarm / murmuration) ============
vec3 formMurmure(vec2 uv, float t, float R){
  vec3 col=vec3(0.0);
  float r=length(uv);
  float Rb=R*(1.0+uBreath*0.10);
  if(r>Rb*1.9) return col;
  float scatter = uThink*0.5 + uError*0.4;   // swarm loosens when thinking / erroring
  for(int i=0;i<7;i++){
    float fi=float(i)/6.0;
    float sr=Rb*(0.40 + fi*0.72 + scatter*0.4);
    if(r>sr) continue;
    float z=sqrt(max(sr*sr-r*r,0.0));
    float zS=mod(float(i),2.0)<1.0?1.0:-1.0;
    vec3 p=vec3(uv, z*zS);
    p.xy += uGaze*0.025*uAttention;            // flock leans toward gaze
    float rs=0.10+fi*0.09 + scatter*0.2;
    p.xz=rot(t*rs)*p.xz;
    p.yz=rot(t*rs*0.6+fi)*p.yz;
    // murmuration density wave passing through the flock
    float wave=sin(p.x*6.0 + p.y*4.0 - t*1.4 + fbm(p*2.0+t*0.3)*4.0);
    vec3 g=floor(p*24.0); vec3 f=fract(p*24.0)-0.5;
    float md=1.0;
    for(int dx=-1;dx<=1;dx++){
      for(int dy=-1;dy<=1;dy++){
        vec3 o=vec3(float(dx),float(dy),0.0);
        vec3 c=hash33(g+o); c.z=0.0;
        vec3 dd=o+c*0.5-f; dd.z=0.0;
        md=min(md, dot(dd,dd));
      }
    }
    float spark=exp(-md*130.0)*(0.5+0.5*wave);
    float depth=(z*zS/Rb)*0.5+0.5;
    vec3 c=mix(uAccent2*0.7, uAccent, depth);
    col+=spark*c*(0.9-fi*0.08)*(0.55+uBreath*0.6);
  }
  col+=uAccent*exp(-r*r*45.0)*0.20*(0.55+uBreath*0.55);   // soft heart
  return col;
}

// ============ FORM 3 — NEBULEUSE (true ray-traced 3D glass orb) ============
// A real sphere is intersected per-pixel; satellite "dashes" live on tilted,
// precessing 3D rings and are re-sampled through the glass via refraction.
// Count / speed / grouping are driven by the live state weights.
#define TAU 6.28318530718
#define NEB_MAX_RINGS 6
#define NEB_MAX_DASHES 16
const float NEB_R    = 1.0;     // sphere radius (world)
const float NEB_CAMZ = 3.6;
const float NEB_FOV  = 0.92;
const float NEB_CORE = 0.20;    // diffuse-light iris size

mat3 rotXm(float a){ float c=cos(a),s=sin(a); return mat3(1.0,0.0,0.0, 0.0,c,-s, 0.0,s,c); }
mat3 rotYm(float a){ float c=cos(a),s=sin(a); return mat3(c,0.0,s, 0.0,1.0,0.0, -s,0.0,c); }

// shortest distance between ray (ro,rd) and segment a-b; closest t along ray in tRay,
// and the fractional position along the segment (0..1) in sSeg
float segRay(vec3 ro, vec3 rd, vec3 a, vec3 b, out float tRay, out float sSeg){
  vec3 ba=b-a, r=ro-a;
  float baba=dot(ba,ba), rdba=dot(rd,ba), rdr=dot(rd,r), bar=dot(ba,r);
  float denom=baba-rdba*rdba;
  float s=(denom>1e-6)?clamp((bar-rdba*rdr)/denom,0.0,1.0):0.0;
  float t=max(-rdr+s*rdba,0.0);
  vec3 pr=ro+rd*t, ps=a+ba*s;
  tRay=t; sSeg=s;
  return length(pr-ps);
}

#define NEB_MAX_COMETS 28
#define NEB_TRAIL 14
// Each "comet" is an independent shooting star on its OWN orbital axis, drawn as
// ONE continuous tapering streak: we find the nearest point along the whole orbit
// arc, then fade smoothly by its position along the trail (no segmented beads).
vec3 nebGlow(vec3 ro, vec3 rd, float tNear, float tFar,
             float cometCount, float trailScale, float thick){
  vec3 acc=vec3(0.0);
  for(int c=0;c<NEB_MAX_COMETS;c++){
    if(float(c)>=cometCount) break;
    float fc=float(c);
    float h =hash11(fc*1.37+3.1);
    float h2=hash11(fc*2.11+7.7);
    float h3=hash11(fc*4.53+1.9);
    // a unique, slowly precessing orbital axis for every comet
    float pa=h*TAU + uTime*0.06*(0.3+h);
    // bounded tilt (static offset + gentle wobble) so scaling by equator is smooth
    float pb=((h2-0.5)*3.4 + 0.4*sin(uTime*0.25*(0.4+h2)))*(1.0-uEquator);  // → equatorial plane
    vec3 axis=normalize(vec3(sin(pb)*cos(pa), cos(pb), sin(pb)*sin(pa)));
    vec3 ref =abs(axis.y)<0.9?vec3(0.0,1.0,0.0):vec3(1.0,0.0,0.0);
    vec3 u=normalize(cross(axis,ref));
    vec3 v=cross(axis,u);
    float Rr=NEB_R*(uTrailAlt + 0.06*h3);            // altitude (tweak) + tiny spread
    float dir=1.0;                                    // all comets orbit the same way
    float head=uOrbitPhase*(0.55+1.15*h) + h*TAU;     // monotonic phase → no reversal
    float trailLen=trailScale*(0.6+0.9*h2);
    // walk the arc, keep only the single closest approach (→ one smooth tube)
    float bestD=1e9, bestF=0.0;
    bool  hit=false;
    vec3  prev=Rr*(cos(head)*u + sin(head)*v);
    for(int k=1;k<=NEB_TRAIL;k++){
      float fk=float(k);
      float th2=head - (fk/float(NEB_TRAIL))*trailLen*dir;
      vec3 cur=Rr*(cos(th2)*u + sin(th2)*v);
      float tr, sSeg;
      float d=segRay(ro,rd,prev,cur,tr,sSeg);
      prev=cur;
      if(tr<tNear||tr>tFar) continue;
      if(d<bestD){ bestD=d; bestF=(fk-1.0+sSeg)/float(NEB_TRAIL); hit=true; }
    }
    if(hit){
      float taper=pow(1.0-bestF, 1.35);              // 1 at head → 0 at tail, smooth
      float tw=thick*(0.22 + 1.0*taper);             // streak thins toward the tail
      float g=exp(-(bestD*bestD)/(tw*tw)) * (0.10 + 0.95*taper) * uTrailGlow;
      vec3  cc=mix(uAccent, uAccent3, smoothstep(0.6, 1.0, taper));  // whiter at the head
      acc += cc*g;
    }
  }
  return acc;
}

vec3 nebBg(vec2 p){
  float r=length(p);
  vec3 col=uBg;
  col += uAccent*0.05*exp(-r*r*2.4);   // faint halo behind orb
  col *= 1.0-0.28*r*r;                  // vignette
  return col;
}

vec4 renderNeb3D(vec2 frag){
  vec2 p=(frag-0.5*uRes)/uRes.y;
  vec3 ro=vec3(0.0,0.0,NEB_CAMZ);
  vec2 pz=p/uSphereSize;                          // sphere size (zoom)
  vec3 rd=normalize(vec3(pz*NEB_FOV,-1.0));
  // NOTE: camera is fixed — the gaze must rotate only the INNER sphere, not the
  // satellites — so no global scene rotation is applied here.

  // ---- everything is driven by the eased per-state preset uniforms ----
  // only the rim halo and the brume react to the live voice level (uAudio).
  float voice=uAudio;
  float cometCount=uTrailCount;
  float trailScale=uTrailLen;
  float thick=uTrailWidth;
  float coreGlow=uCoreGlowT*(0.85+uBreath*0.25+uAttention*0.25);
  float ior=uIorT;

  vec3 col;
  float alpha;
  float b=dot(ro,rd);
  float cc=dot(ro,ro)-NEB_R*NEB_R;
  float disc=b*b-cc;
  float tHit=disc>0.0?-b-sqrt(disc):-1.0;

  if(tHit>0.0){
    vec3 frontGlow=nebGlow(ro,rd,0.0,tHit,cometCount,trailScale,thick);
    vec3 pos=ro+rd*tHit;
    vec3 n=normalize(pos);
    float fres=pow(1.0-max(dot(-rd,n),0.0),4.0);
    // refract through the glass and re-sample the dash field behind it
    vec3 rdr=refract(rd,n,1.0/ior);
    vec3 backGlow=nebGlow(pos,rdr,0.0,12.0,cometCount,trailScale,thick)*uTint*0.30;
    // diffuse-light iris — soft glowing core that leans toward the gaze
    vec3 coreOff=vec3(uGaze.x, -uGaze.y, 0.0)*NEB_R*0.42;   // only the inner light tracks the cursor
    float tcore=max(dot(coreOff-pos, rdr), 0.0);
    float dCore=length(pos+rdr*tcore-coreOff);
    float core=coreGlow*exp(-(dCore*dCore)/(NEB_CORE*NEB_CORE));
    core+=coreGlow*0.12*exp(-dCore/(NEB_CORE*2.6));
    // misty interior — fog rotates with the gaze, so the inner sphere turns
    mat3 Rin=rotYm(uGaze.x*0.6)*rotXm(-uGaze.y*0.45);
    vec3 fp=Rin*(pos+rdr*0.6);
    float fog=fbm(fp*2.0 + vec3(0.0,0.0,uTime*0.12));
    // speak: the brume swings wide with the voice (near-empty on silences, billowing on peaks)
    float fogAmt=uFogAmt*mix(1.0, 0.25 + voice*2.6, uSpeak);
    vec3 glass=backGlow + uAccent3*core;
    glass+=uAccent2*pow(fog,1.6)*0.18*fogAmt*(0.6+uBreath*0.5);   // brume in the body
    glass+=uAccent*fres*1.1*uRimT*mix(1.0, 0.2 + voice*2.8, uSpeak); // rim halo swings wide with the voice
    vec3 L=normalize(vec3(0.55,0.7,0.55));
    vec3 hv=normalize(L-rd);
    float spec=pow(max(dot(n,hv),0.0),80.0);
    glass+=vec3(1.0)*spec*0.85;
    glass+=uTint*0.05;                                         // faint glassy body fill
    col=glass+frontGlow;
    // translucent glass body: a soft disc, brighter at the rim, opaque where lit
    float lum=max(max(col.r,col.g),col.b);
    alpha=clamp(0.16 + 0.55*fres + lum, 0.0, 1.0);
  } else {
    vec3 halo=uAccent*0.05*exp(-length(p)*length(p)*2.4);     // faint halo behind orb
    col=halo + nebGlow(ro,rd,0.0,24.0,cometCount,trailScale,thick);
    alpha=clamp(max(max(col.r,col.g),col.b)*1.5, 0.0, 1.0);   // only the glow shows; rest is transparent
  }

  if(uError>0.001){ col.r*=1.0+uError*0.30; col.b*=1.0-uError*0.18; }
  col=vec3(1.0)-exp(-col*1.3);                                // soft tonemap
  col += (hash21(frag+floor(uTime*60.0))-0.5)/255.0;          // dither
  return vec4(col, alpha);
}

// ---- subtle state overlays around the body ----
vec3 listenRings(vec2 uv, float t, float R){
  float r=length(uv);
  if(r<R*0.7||r>R*2.0) return vec3(0.0);
  float ang=atan(uv.y,uv.x);
  float w=0.0;
  for(int i=0;i<4;i++){
    float fi=float(i);
    float wr=R*(1.18+fi*0.14)+sin(ang*(14.0+fi*4.0)+t*(0.9+fi*0.2)*3.0)*0.015;
    w+=exp(-pow((r-wr)/0.008,2.0))*(1.0-fi*0.18);
  }
  return uAccent*w*0.8;
}
vec3 speakRings(vec2 uv, float t, float R, float audio){
  float r=length(uv);
  if(r<R*0.9||r>R*2.4) return vec3(0.0);
  float w=0.0;
  for(int i=0;i<4;i++){
    float fi=float(i);
    float phase=mod(t*0.6+fi*0.4,1.0);
    float wr=R*(1.0+phase*1.5);
    float amp=(1.0-phase)*(0.35+audio*0.65);
    w+=exp(-pow((r-wr)/(0.02+phase*0.04),2.0))*amp;
  }
  return uAccent*w*1.0;
}

vec2 errorOffset(vec2 uv, float t, float s){
  if(s<0.001) return uv;
  float band=step(0.85, hash21(vec2(floor(uv.y*30.0), floor(t*40.0))));
  float jx=(hash21(vec2(floor(t*22.0), floor(uv.y*20.0)))-0.5)*0.06*s*band;
  return uv+vec2(jx,0.0);
}

void main(){
  vec2 frag=gl_FragCoord.xy;
  if(uForm==3){ fragColor=renderNeb3D(frag); return; }
  vec2 uv=(frag-uRes*0.5)/min(uRes.x,uRes.y);
  // floating drift + a touch of gaze lean for the whole body
  uv -= uDrift;
  uv -= uGaze*0.012*uAttention;
  float t=uTime*(0.4+uMotion*0.9);
  float R=0.205;

  uv = errorOffset(uv, uTime, uError);

  vec3 col=vec3(0.0);
  if(uForm==0) col=formSouffle(uv,t,R);
  else if(uForm==1) col=formIris(uv,t,R);
  else col=formMurmure(uv,t,R);

  col += listenRings(uv,t,R)*uListen;
  col += speakRings(uv,t,R,uAudio)*uSpeak;

  // idle breath halo
  float r=length(uv);
  float halo=exp(-pow((r-R*1.06)/(0.07+uBreath*0.05),2.0));
  col += uAccent*halo*0.18*uIdle*(0.5+uBreath*0.5);

  // alert warm pulse
  if(uAlert>0.001){
    float pulse=0.7+0.3*sin(uTime*7.5);
    col=mix(col, col*vec3(1.0,0.62,0.18)*pulse*1.25, uAlert*0.8);
  }
  // error scan + chroma split
  if(uError>0.001){
    float scan=step(0.5, sin(uv.y*200.0+uTime*30.0));
    col+=uAccent*scan*uError*0.07;
    col.r*=1.0+uError*0.4; col.b*=1.0+uError*0.18;
  }

  col *= 0.85 + uGlow*0.7;
  float vign=smoothstep(1.3,0.25,length(uv*vec2(uRes.x/uRes.y,1.0)));
  col *= 0.9 + vign*0.18;

  vec3 finalC=uBg + col;
  float sl=0.5+0.5*sin(frag.y*1.3);
  finalC*=0.985+sl*0.015;
  float grain=(hash21(frag+floor(uTime*60.0))-0.5)*0.018;
  finalC+=grain;
  fragColor=vec4(finalC,1.0);
}`;

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type);
  if (!s) throw new Error("Shader allocation failed");
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(s) ?? "(no info)";
    throw new Error(`Shader compile failed: ${info}`);
  }
  return s;
}

// Uniform names match the GLSL above. Built as a string→location map so the
// renderer can look each up once at link time (verbatim list from the mockup).
const UNIFORM_NAMES = [
  "uRes",
  "uTime",
  "uForm",
  "uMotion",
  "uGlow",
  "uAccent",
  "uAccent2",
  "uAccent3",
  "uBg",
  "uAudio",
  "uBreath",
  "uGaze",
  "uAttention",
  "uBlink",
  "uDrift",
  "uWobble",
  "uIdle",
  "uListen",
  "uThink",
  "uSpeak",
  "uAlert",
  "uError",
  "uTrailCount",
  "uTrailSpeed",
  "uTrailLen",
  "uTrailWidth",
  "uTrailAlt",
  "uTrailGlow",
  "uSphereSize",
  "uCoreGlowT",
  "uFogAmt",
  "uIorT",
  "uRimT",
  "uEquator",
  "uLatitude",
  "uOrbitPhase",
  "uTint",
] as const;

type UniformName = (typeof UNIFORM_NAMES)[number];
type UniformMap = Record<UniformName, WebGLUniformLocation | null>;

/**
 * Create a WebGL2 renderer for the conscience shader. Returns `null` if the
 * canvas cannot provide a WebGL2 context (the caller renders an HTML-side error
 * banner in that case — same contract as `sphere/sphereShader.ts`). The mockup
 * threw on no-WebGL2; we soften that to a null so the React tree degrades
 * gracefully instead of crashing.
 */
export function createConscienceRenderer(canvas: HTMLCanvasElement): ConscienceRenderer | null {
  const gl = canvas.getContext("webgl2", {
    antialias: true,
    premultipliedAlpha: false,
    preserveDrawingBuffer: true,
  }) as WebGL2RenderingContext | null;
  if (!gl) return null;

  const prog = gl.createProgram();
  if (!prog) return null;

  gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
  gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(prog) ?? "(no info)";
    throw new Error(`Program link failed: ${info}`);
  }
  gl.useProgram(prog);

  // Full-screen quad
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  // prettier-ignore
  gl.bufferData(
    gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
    gl.STATIC_DRAW,
  );
  const loc = gl.getAttribLocation(prog, "aPos");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  const U = {} as UniformMap;
  for (const name of UNIFORM_NAMES) {
    U[name] = gl.getUniformLocation(prog, name);
  }

  function setSize(width: number, height: number, dpr: number): void {
    const W = Math.floor(width * dpr);
    const H = Math.floor(height * dpr);
    if (canvas.width !== W) canvas.width = W;
    if (canvas.height !== H) canvas.height = H;
    gl.viewport(0, 0, W, H);
    gl.uniform2f(U.uRes, W, H);
  }

  function render(p: ConscienceRenderParams): void {
    gl.uniform1f(U.uTime, p.time);
    gl.uniform1i(U.uForm, p.form);
    gl.uniform1f(U.uMotion, p.motion);
    gl.uniform1f(U.uGlow, p.glow);
    gl.uniform3f(U.uAccent, p.accent[0], p.accent[1], p.accent[2]);
    gl.uniform3f(U.uAccent2, p.accent2[0], p.accent2[1], p.accent2[2]);
    gl.uniform3f(U.uAccent3, p.accent3[0], p.accent3[1], p.accent3[2]);
    gl.uniform3f(U.uBg, p.bg[0], p.bg[1], p.bg[2]);
    gl.uniform1f(U.uAudio, p.audio);
    gl.uniform1f(U.uBreath, p.breath);
    gl.uniform2f(U.uGaze, p.gaze[0], p.gaze[1]);
    gl.uniform1f(U.uAttention, p.attention);
    gl.uniform1f(U.uBlink, p.blink);
    gl.uniform2f(U.uDrift, p.drift[0], p.drift[1]);
    gl.uniform1f(U.uWobble, p.wobble);
    gl.uniform1f(U.uIdle, p.states.idle);
    gl.uniform1f(U.uListen, p.states.listen);
    gl.uniform1f(U.uThink, p.states.think);
    gl.uniform1f(U.uSpeak, p.states.speak);
    gl.uniform1f(U.uAlert, p.states.alert);
    gl.uniform1f(U.uError, p.states.error);
    const n = p.neb;
    gl.uniform1f(U.uTrailCount, n.trailCount);
    gl.uniform1f(U.uTrailSpeed, n.trailSpeed);
    gl.uniform1f(U.uTrailLen, n.trailLen);
    gl.uniform1f(U.uTrailWidth, n.trailWidth);
    gl.uniform1f(U.uTrailAlt, n.trailAlt);
    gl.uniform1f(U.uTrailGlow, n.trailGlow);
    gl.uniform1f(U.uSphereSize, n.sphereSize);
    gl.uniform1f(U.uCoreGlowT, n.coreGlow);
    gl.uniform1f(U.uFogAmt, n.fogAmt);
    gl.uniform1f(U.uIorT, n.ior);
    gl.uniform1f(U.uRimT, n.rim);
    gl.uniform1f(U.uEquator, n.equator);
    gl.uniform1f(U.uLatitude, n.latitude);
    gl.uniform1f(U.uOrbitPhase, n.orbitPhase);
    gl.uniform3f(U.uTint, p.tint[0], p.tint[1], p.tint[2]);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }

  return { setSize, render };
}
