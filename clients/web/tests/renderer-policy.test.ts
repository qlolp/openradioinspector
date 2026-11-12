// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SpectrumRenderer } from '../src/spectrum-renderer';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { Waterfall } from '../src/charts';
import { t } from '../src/i18n';

const renderers: SpectrumRenderer[] = [];
beforeEach(() => {
  vi.stubGlobal(
    'requestAnimationFrame',
    vi.fn(() => 1),
  );
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
});
afterEach(() => {
  for (const renderer of renderers.splice(0)) renderer.dispose();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function fixture(renderer: unknown, extension = true) {
  const identity = { renderer, extension };
  const gl = {
    VERTEX_SHADER: 0x8b31,
    FRAGMENT_SHADER: 0x8b30,
    COMPILE_STATUS: 0x8b81,
    LINK_STATUS: 0x8b82,
    ARRAY_BUFFER: 0x8892,
    FLOAT: 0x1406,
    isContextLost: vi.fn(() => false),
    getExtension: vi.fn(() => (identity.extension ? { UNMASKED_RENDERER_WEBGL: 0x9246 } : null)),
    getParameter: vi.fn(() => identity.renderer),
    createShader: vi.fn(() => ({})),
    shaderSource: vi.fn(),
    compileShader: vi.fn(),
    getShaderParameter: vi.fn(() => true),
    deleteShader: vi.fn(),
    createProgram: vi.fn(() => ({})),
    attachShader: vi.fn(),
    linkProgram: vi.fn(),
    getProgramParameter: vi.fn(() => true),
    createVertexArray: vi.fn(() => ({})),
    createBuffer: vi.fn(() => ({})),
    bindVertexArray: vi.fn(),
    bindBuffer: vi.fn(),
    getAttribLocation: vi.fn(() => 0),
    enableVertexAttribArray: vi.fn(),
    vertexAttribPointer: vi.fn(),
    getUniformLocation: vi.fn(() => ({})),
    deleteBuffer: vi.fn(),
    deleteProgram: vi.fn(),
    deleteVertexArray: vi.fn(),
    TEXTURE_2D: 0x0de1,
    RGBA8: 0x8058,
    TEXTURE_MIN_FILTER: 0x2801,
    TEXTURE_MAG_FILTER: 0x2800,
    NEAREST: 0x2600,
    TRIANGLES: 4,
    createTexture: vi.fn(() => ({})),
    bindTexture: vi.fn(),
    texStorage2D: vi.fn(),
    texParameteri: vi.fn(),
    texSubImage2D: vi.fn(),
    deleteTexture: vi.fn(),
    viewport: vi.fn(),
    useProgram: vi.fn(),
    uniform1i: vi.fn(),
    drawArrays: vi.fn(),
  };
  const canvas = document.createElement('canvas'),
    fallback = document.createElement('canvas');
  vi.spyOn(canvas, 'getContext').mockReturnValue(gl as unknown as WebGL2RenderingContext);
  vi.spyOn(fallback, 'getContext').mockReturnValue({} as CanvasRenderingContext2D);
  const modes: string[] = [];
  const graph = new SpectrumRenderer(canvas, fallback, (mode) => modes.push(mode));
  renderers.push(graph);
  return { graph, modes, gl, identity };
}

it('uses the existing Canvas fallback for the observed SwiftShader driver without compiling GL shaders', () => {
  const { modes, gl } = fixture(
    'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)',
  );
  expect(modes.at(-1)).toBe('Canvas fallback');
  expect(gl.compileShader).not.toHaveBeenCalled();
  expect(gl.createProgram).not.toHaveBeenCalled();
});

it.each([
  'ANGLE (Intel, Mesa Intel UHD)',
  'ANGLE (NVIDIA RTX)',
  'SwiftShaderish unknown renderer',
  '',
  undefined,
  17,
])('retains normal WebGL initialization for an unclassified renderer %s', (renderer) => {
  const { modes, gl } = fixture(renderer);
  expect(modes.at(-1)).toBe('WebGL2');
  expect(gl.compileShader).toHaveBeenCalledTimes(2);
});

it('retains normal WebGL when the debug identity extension is unavailable', () => {
  const { modes, gl } = fixture('SwiftShader', false);
  expect(modes.at(-1)).toBe('WebGL2');
  expect(gl.getParameter).not.toHaveBeenCalled();
  expect(gl.compileShader).toHaveBeenCalledTimes(2);
});

it('queries identity only at initialization/restoration and reevaluates a restored context', () => {
  const { graph, modes, gl, identity } = fixture('SwiftShader');
  expect(modes.at(-1)).toBe('Canvas fallback');
  graph.update(undefined, undefined, { low: 0, high: 1 });
  graph.resize(800, 400);
  graph.theme();
  expect(gl.getExtension).toHaveBeenCalledTimes(1);
  identity.renderer = 'ANGLE (AMD Radeon hardware)';
  graph.restore();
  expect(modes.at(-1)).toBe('WebGL2');
  expect(gl.compileShader).toHaveBeenCalledTimes(2);
  expect(gl.getExtension).toHaveBeenCalledTimes(2);
  identity.renderer = 'SwiftShader';
  graph.restore();
  expect(modes.at(-1)).toBe('Canvas fallback');
  expect(gl.compileShader).toHaveBeenCalledTimes(2);
  expect(gl.getExtension).toHaveBeenCalledTimes(3);
});

it('uses Canvas for the SwiftShader Waterfall without hidden texture uploads and rechecks restoration', async () => {
  const { gl, graph, identity } = fixture('SwiftShader');
  graph.dispose();
  renderers.splice(renderers.indexOf(graph), 1);
  gl.compileShader.mockClear();
  gl.createProgram.mockClear();
  const context = {
    canvas: document.createElement('canvas'),
    putImageData: vi.fn(),
    drawImage: vi.fn(),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(((kind: string) =>
    kind === 'webgl2' ? gl : context) as unknown as HTMLCanvasElement['getContext']);
  vi.stubGlobal(
    'ImageData',
    class {
      constructor(
        public data: Uint8ClampedArray,
        public width: number,
        public height: number,
      ) {}
    },
  );
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  document.body.append(element);
  const root = createRoot(element);
  try {
    await act(() => root.render(createElement(Waterfall, { rows: [] })));
    expect(element.querySelector('[data-testid="renderer-state"]')?.textContent).toBe(
      t('Canvas fallback'),
    );
    expect(gl.compileShader).not.toHaveBeenCalled();
    expect(gl.texSubImage2D).not.toHaveBeenCalled();
    expect(gl.drawArrays).not.toHaveBeenCalled();
    expect(context.putImageData).toHaveBeenCalled();
    const primary = element.querySelector('canvas')!;
    identity.renderer = 'ANGLE (AMD Radeon hardware)';
    await act(() => {
      primary.dispatchEvent(new Event('webglcontextrestored'));
    });
    expect(element.querySelector('[data-testid="renderer-state"]')?.textContent).toBe(
      t('WebGL2 · incremental rows'),
    );
    expect(gl.compileShader).toHaveBeenCalledTimes(2);
    expect(gl.drawArrays).toHaveBeenCalledTimes(1);
    identity.renderer = 'SwiftShader';
    await act(() => {
      primary.dispatchEvent(new Event('webglcontextrestored'));
    });
    expect(element.querySelector('[data-testid="renderer-state"]')?.textContent).toBe(
      t('Canvas fallback'),
    );
    expect(gl.compileShader).toHaveBeenCalledTimes(2);
    expect(gl.drawArrays).toHaveBeenCalledTimes(1);
  } finally {
    await act(() => root.unmount());
    element.remove();
    delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
  }
});
