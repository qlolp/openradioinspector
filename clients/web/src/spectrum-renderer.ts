import type { Comparison, DisplaySweep } from './types';
import { peakPosition, sourceLevels, traceVertices, type FrequencyRange } from './spectrum-view';
const GRID = new Float32Array(
  Array.from({ length: 5 }, (_, i) => {
    const p = i / 2 - 1;
    return [p, -1, p, 1, -1, p, 1, p];
  }).flat(),
);
export function spectrumColors(css: Pick<CSSStyleDeclaration, 'getPropertyValue'>) {
  return {
    signal: css.getPropertyValue('--signal').trim() || '#66dbc1',
    grid: css.getPropertyValue('--line').trim() || '#303d4b',
    baseline95: css.getPropertyValue('--warn').trim() || '#b17300',
    baseline50: '#6f8de6',
  };
}

export class SpectrumRenderer {
  private gl: WebGL2RenderingContext | null;
  private context: CanvasRenderingContext2D;
  private program: WebGLProgram | null = null;
  private buffer: WebGLBuffer | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private rgba: WebGLUniformLocation | null = null;
  private sweep?: DisplaySweep;
  private comparison?: Comparison;
  private baseline?: { frequencies: number[]; p95: number[]; p50: number[] };
  private range: FrequencyRange = { low: 88_000_000, high: 108_000_000 };
  private raf = 0;
  private width = 1;
  private height = 1;
  private scale = 1;
  private colors = spectrumColors({ getPropertyValue: () => '' });
  private disposed = false;
  private sourceGeneration = 0;
  private drawnSource = '';
  private active?: HTMLCanvasElement;
  private frame = 0;
  private acknowledgements = new Set<number>();
  constructor(
    private canvas: HTMLCanvasElement,
    private fallback: HTMLCanvasElement,
    private report: (mode: string) => void,
  ) {
    this.context = fallback.getContext('2d')!;
    this.gl = canvas.getContext('webgl2', {
      alpha: true,
      antialias: false,
      preserveDrawingBuffer: false,
    });
    this.initialize();
    this.theme();
  }
  private initialize() {
    const gl = this.gl;
    if (!gl || gl.isContextLost()) {
      this.report('Canvas fallback');
      return;
    }
    const shader = (kind: number, source: string) => {
      const result = gl.createShader(kind)!;
      gl.shaderSource(result, source);
      gl.compileShader(result);
      if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) {
        gl.deleteShader(result);
        throw Error('Spectrum shader unavailable');
      }
      return result;
    };
    try {
      const vertex = shader(
        gl.VERTEX_SHADER,
        '#version 300 es\nin vec2 point;void main(){gl_Position=vec4(point,0.,1.);gl_PointSize=6.;}',
      );
      const fragment = shader(
        gl.FRAGMENT_SHADER,
        '#version 300 es\nprecision highp float;uniform vec4 rgba;out vec4 color;void main(){color=rgba;}',
      );
      this.program = gl.createProgram()!;
      gl.attachShader(this.program, vertex);
      gl.attachShader(this.program, fragment);
      gl.linkProgram(this.program);
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
      if (!gl.getProgramParameter(this.program, gl.LINK_STATUS))
        throw Error('Spectrum program unavailable');
      this.vao = gl.createVertexArray();
      this.buffer = gl.createBuffer();
      gl.bindVertexArray(this.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      const point = gl.getAttribLocation(this.program, 'point');
      gl.enableVertexAttribArray(point);
      gl.vertexAttribPointer(point, 2, gl.FLOAT, false, 0, 0);
      this.rgba = gl.getUniformLocation(this.program, 'rgba');
      this.report('WebGL2');
    } catch {
      this.program = null;
      this.report('Canvas fallback');
    }
  }
  update(
    sweep: DisplaySweep | undefined,
    comparison: Comparison | undefined,
    range: FrequencyRange,
  ) {
    if (comparison !== this.comparison)
      this.baseline = comparison?.compatible
        ? {
            frequencies: comparison.items.map((item) => item.frequency_hz),
            p95: comparison.items.map((item) => item.baseline_p95_dbm),
            p50: comparison.items.map((item) => item.baseline_p50_dbm),
          }
        : undefined;
    this.sweep = sweep;
    this.comparison = comparison;
    this.range = range;
    this.schedule();
  }
  resize(width: number, height: number) {
    this.width = Math.max(1, width);
    this.height = Math.max(1, height);
    this.scale = Math.min(devicePixelRatio || 1, 2);
    this.schedule();
  }
  theme() {
    const css = getComputedStyle(this.canvas);
    this.colors = spectrumColors(css);
    this.schedule();
  }
  lost() {
    this.program = null;
    this.invalidateCanvas();
    this.report('Canvas fallback · graphics context lost');
    this.schedule();
  }
  restore() {
    this.initialize();
    this.invalidateCanvas();
    this.schedule();
  }
  private invalidateCanvas() {
    this.sourceGeneration++;
    this.drawnSource = '';
    delete this.canvas.dataset.sweepId;
    delete this.fallback.dataset.sweepId;
  }
  private schedule() {
    if (!this.raf && !this.disposed)
      this.raf = requestAnimationFrame(() => {
        this.raf = 0;
        this.draw();
      });
  }
  private draw() {
    if (this.disposed) return;
    const gl = this.gl && this.program && !this.gl.isContextLost() ? this.gl : null;
    const canvas = gl ? this.canvas : this.fallback;
    const pixelWidth = Math.max(1, Math.round(this.width * this.scale)),
      pixelHeight = Math.max(1, Math.round(this.height * this.scale));
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    const bounds = sourceLevels(this.sweep);
    if (gl) {
      gl.viewport(0, 0, pixelWidth, pixelHeight);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(this.program);
      gl.bindVertexArray(this.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    } else {
      this.context.setTransform(this.scale, 0, 0, this.scale, 0, 0);
      this.context.clearRect(0, 0, this.width, this.height);
    }
    const line = (vertices: Float32Array, color: string, points = false, segments = false) => {
      if (!vertices.length) return;
      if (gl) {
        const hex = color.replace('#', '');
        const value = Number.parseInt(hex, 16);
        gl.uniform4f(
          this.rgba,
          ((value >>> 16) & 255) / 255,
          ((value >>> 8) & 255) / 255,
          (value & 255) / 255,
          1,
        );
        gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.DYNAMIC_DRAW);
        gl.drawArrays(
          points ? gl.POINTS : segments ? gl.LINES : gl.LINE_STRIP,
          0,
          vertices.length / 2,
        );
      } else {
        const ctx = this.context;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (let i = 0; i < vertices.length; i += 2) {
          const x = ((vertices[i] + 1) / 2) * this.width,
            y = ((1 - vertices[i + 1]) / 2) * this.height;
          if (points) {
            ctx.moveTo(x + 3, y);
            ctx.arc(x, y, 3, 0, Math.PI * 2);
          } else if (i && !(segments && i % 4 === 0)) ctx.lineTo(x, y);
          else ctx.moveTo(x, y);
        }
        if (points) ctx.fill();
        else ctx.stroke();
      }
    };
    line(GRID, this.colors.grid, false, true);
    if (this.baseline) {
      line(
        traceVertices(this.baseline.frequencies, this.baseline.p95, this.range, bounds),
        this.colors.baseline95,
      );
      line(
        traceVertices(this.baseline.frequencies, this.baseline.p50, this.range, bounds),
        this.colors.baseline50,
      );
    }
    if (this.sweep) {
      line(
        traceVertices(this.sweep.frequencies_hz, this.sweep.levels, this.range, bounds),
        this.colors.signal,
      );
      const peak = peakPosition(this.sweep, this.range, bounds);
      if (peak) line(new Float32Array(peak), this.colors.signal, true);
    }
    const id = this.sweep?.id ?? '';
    if (this.active !== canvas || this.drawnSource !== id) {
      this.sourceGeneration++;
      this.drawnSource = id;
      this.active = canvas;
      delete this.canvas.dataset.sweepId;
      delete this.fallback.dataset.sweepId;
    }
    if (!id || this.width <= 1 || this.height <= 1) return;
    const sourceGeneration = this.sourceGeneration,
      frame = ++this.frame;
    // One scheduled draw per animation frame. Interactions do not starve source
    // acknowledgement; two RAFs retain the existing conservative paint barrier.
    const first = requestAnimationFrame(() => {
      this.acknowledgements.delete(first);
      const second = requestAnimationFrame(() => {
        this.acknowledgements.delete(second);
        if (this.disposed || sourceGeneration !== this.sourceGeneration || this.active !== canvas)
          return;
        if (canvas.dataset.sweepId !== id) canvas.dataset.sweepId = id;
        canvas.dataset.renderFrame = String(frame);
      });
      this.acknowledgements.add(second);
    });
    this.acknowledgements.add(first);
  }
  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    for (const id of this.acknowledgements) cancelAnimationFrame(id);
    this.acknowledgements.clear();
    this.gl?.deleteBuffer(this.buffer);
    this.gl?.deleteProgram(this.program);
    this.gl?.deleteVertexArray(this.vao);
  }
}
