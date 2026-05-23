import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SphereCanvas, type SphereCanvasProps } from "./SphereCanvas";

// --- Minimal WebGL2 stub --------------------------------------------------
// jsdom does not implement WebGL. The renderer in sphereShader.ts only calls a
// fixed set of methods on the gl context; we stub each one and add a
// `__renderCalls` counter that drawArrays bumps so tests can assert the loop
// actually rendered.

type GlStub = {
  VERTEX_SHADER: number;
  FRAGMENT_SHADER: number;
  ARRAY_BUFFER: number;
  STATIC_DRAW: number;
  TRIANGLES: number;
  FLOAT: number;
  COMPILE_STATUS: number;
  LINK_STATUS: number;
  createShader: () => unknown;
  shaderSource: () => void;
  compileShader: () => void;
  getShaderParameter: () => boolean;
  getShaderInfoLog: () => string;
  createProgram: () => unknown;
  attachShader: () => void;
  linkProgram: () => void;
  getProgramParameter: () => boolean;
  getProgramInfoLog: () => string;
  useProgram: () => void;
  createBuffer: () => unknown;
  bindBuffer: () => void;
  bufferData: () => void;
  getAttribLocation: () => number;
  vertexAttribPointer: () => void;
  enableVertexAttribArray: () => void;
  getUniformLocation: () => unknown;
  uniform1f: () => void;
  uniform1i: () => void;
  uniform2f: () => void;
  uniform3f: () => void;
  viewport: () => void;
  clearColor: () => void;
  clear: () => void;
  drawArrays: () => void;
  __renderCalls: number;
};

function make2dStub(): CanvasRenderingContext2D {
  // Only the calls drawGlyphOverlay performs are exercised. Everything is a
  // no-op; the goal is to not throw inside the rAF loop.
  const noop = (): void => undefined;
  return {
    clearRect: noop,
    save: noop,
    restore: noop,
    translate: noop,
    fillText: noop,
    set font(_v: string) {},
    set textAlign(_v: string) {},
    set textBaseline(_v: string) {},
    set fillStyle(_v: string) {},
    set shadowColor(_v: string) {},
    set shadowBlur(_v: number) {},
    // biome-ignore lint/suspicious/noExplicitAny: minimal 2D ctx stub
  } as any;
}

function makeGlStub(): GlStub {
  const stub: GlStub = {
    VERTEX_SHADER: 1,
    FRAGMENT_SHADER: 2,
    ARRAY_BUFFER: 3,
    STATIC_DRAW: 4,
    TRIANGLES: 5,
    FLOAT: 6,
    COMPILE_STATUS: 7,
    LINK_STATUS: 8,
    createShader: () => ({}),
    shaderSource: () => undefined,
    compileShader: () => undefined,
    getShaderParameter: () => true,
    getShaderInfoLog: () => "",
    createProgram: () => ({}),
    attachShader: () => undefined,
    linkProgram: () => undefined,
    getProgramParameter: () => true,
    getProgramInfoLog: () => "",
    useProgram: () => undefined,
    createBuffer: () => ({}),
    bindBuffer: () => undefined,
    bufferData: () => undefined,
    getAttribLocation: () => 0,
    vertexAttribPointer: () => undefined,
    enableVertexAttribArray: () => undefined,
    getUniformLocation: () => ({}),
    uniform1f: () => undefined,
    uniform1i: () => undefined,
    uniform2f: () => undefined,
    uniform3f: () => undefined,
    viewport: () => undefined,
    clearColor: () => undefined,
    clear: () => undefined,
    drawArrays: () => {
      stub.__renderCalls += 1;
    },
    __renderCalls: 0,
  };
  return stub;
}

// Hold the original prototype getContext to restore between tests.
const originalGetContext = HTMLCanvasElement.prototype.getContext;

type ContextFactory = (type: string) => unknown;

function installContextStub(factory: ContextFactory): void {
  // biome-ignore lint/suspicious/noExplicitAny: jsdom getContext lacks WebGL types
  (HTMLCanvasElement.prototype as any).getContext = function (
    this: HTMLCanvasElement,
    type: string,
  ): unknown {
    return factory(type);
  };
}

const baseProps: SphereCanvasProps = {
  state: "idle",
  variant: 0,
  motion: 0.55,
  glow: 0.7,
  theme: "warm",
  mood: "calm",
};

describe("SphereCanvas", () => {
  let glStub: GlStub;

  beforeEach(() => {
    glStub = makeGlStub();
  });

  afterEach(() => {
    HTMLCanvasElement.prototype.getContext = originalGetContext;
    vi.restoreAllMocks();
  });

  test("mounts without crashing when WebGL2 is available", () => {
    installContextStub((type) => (type === "webgl2" ? glStub : make2dStub()));
    const { container } = render(<SphereCanvas {...baseProps} />);
    expect(container.querySelector(".sphere-stage")).not.toBeNull();
    expect(container.querySelector(".sphere-canvas")).not.toBeNull();
    expect(container.querySelector(".glyph-overlay")).not.toBeNull();
  });

  test("initialises the renderer and drives the rAF loop", async () => {
    installContextStub((type) => (type === "webgl2" ? glStub : make2dStub()));
    const rafSpy = vi.spyOn(globalThis, "requestAnimationFrame");
    render(<SphereCanvas {...baseProps} />);

    // Wait for at least one rAF tick to flush.
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => resolve());
      });
    });

    expect(rafSpy).toHaveBeenCalled();
    expect(glStub.__renderCalls).toBeGreaterThan(0);
  });

  test("cancels the animation frame on unmount", () => {
    installContextStub((type) => (type === "webgl2" ? glStub : make2dStub()));
    const cancelSpy = vi.spyOn(globalThis, "cancelAnimationFrame");
    const { unmount } = render(<SphereCanvas {...baseProps} />);
    unmount();
    expect(cancelSpy).toHaveBeenCalled();
  });

  test("renders the HUD error banner when WebGL2 is unavailable", () => {
    installContextStub((type) => (type === "webgl2" ? null : make2dStub()));
    render(<SphereCanvas {...baseProps} />);
    expect(screen.getByText(/WebGL2 required/i)).toBeInTheDocument();
    expect(document.querySelector(".hud-error")).not.toBeNull();
    expect(document.querySelector(".sphere-canvas")).toBeNull();
  });
});
